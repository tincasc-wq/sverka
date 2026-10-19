from collections.abc import AsyncIterator

import httpx
import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.collectors.runner import poll_rss, poll_telegram
from app.collectors.store import DocumentIn, SaveResult, save_document
from app.core.db import engine
from app.models import Document, DocumentVersion, Source
from tests.fixtures import ARTICLE_HTML, RSS_FEED, TELEGRAM_PAGE


async def _source(session: AsyncSession, handle: str, **kw) -> Source:
    source = Source(type=kw.pop("type", "media"), name=handle, handle_or_domain=handle, **kw)
    session.add(source)
    await session.flush()
    return source


# --- Запись в архив ---------------------------------------------------------------


async def test_same_url_in_another_form_is_not_duplicated(session: AsyncSession) -> None:
    first = DocumentIn(
        url="https://example.ru/news/1?utm_source=a", title="Пожар", text="Горит склад"
    )
    again = DocumentIn(url="http://www.example.ru/news/1/", title="Пожар", text="Горит склад")

    assert await save_document(session, first) == SaveResult.INSERTED
    assert await save_document(session, again) == SaveResult.UNCHANGED
    assert await session.scalar(select(func.count()).select_from(Document)) == 1


async def test_same_text_from_same_source_is_duplicate(session: AsyncSession) -> None:
    src = await _source(session, "example.ru")
    a = DocumentIn(url="https://example.ru/a", source_id=src.id, title="Пожар", text="Горит склад")
    b = DocumentIn(url="https://example.ru/b", source_id=src.id, title="Пожар", text="Горит склад")

    assert await save_document(session, a) == SaveResult.INSERTED
    assert await save_document(session, b) == SaveResult.DUPLICATE


async def test_same_text_from_other_source_is_kept_as_reprint(session: AsyncSession) -> None:
    src1 = await _source(session, "one.ru")
    src2 = await _source(session, "two.ru")
    text = "Горит склад в Подольске"

    await save_document(session, DocumentIn(url="https://one.ru/a", source_id=src1.id, text=text))
    result = await save_document(
        session, DocumentIn(url="https://two.ru/a", source_id=src2.id, text=text)
    )

    assert result == SaveResult.INSERTED  # перепечатка — сигнал для графа, не дубль


async def test_edited_text_is_versioned_and_first_seen_kept(session: AsyncSession) -> None:
    await save_document(session, DocumentIn(url="https://t.me/chan/1", text="Погибли 12 человек"))
    doc = await session.scalar(select(Document))
    first_seen = doc.first_seen_at

    result = await save_document(
        session, DocumentIn(url="https://t.me/chan/1", text="Погибли 21 человек")
    )

    assert result == SaveResult.EDITED
    await session.refresh(doc)
    assert doc.text == "Погибли 21 человек"
    assert doc.first_seen_at == first_seen
    version = await session.scalar(select(DocumentVersion))
    assert version.text == "Погибли 12 человек"
    assert version.change_type == "edit"


# --- Опрос источников -------------------------------------------------------------


@pytest.fixture
async def committed() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """Сборщики сами делают commit, поэтому здесь — настоящие транзакции и очистка после."""
    maker = async_sessionmaker(engine, expire_on_commit=False)
    yield maker
    async with maker() as s:
        await s.execute(Document.__table__.delete())
        await s.execute(Source.__table__.delete())
        await s.commit()


def _transport(routes: dict[str, bytes]) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url in routes:
            return httpx.Response(200, content=routes[url])
        if url.startswith("https://broken.ru"):
            return httpx.Response(500)
        return httpx.Response(404)

    return httpx.MockTransport(handler)


async def test_poll_rss_saves_full_text_and_isolates_broken_feed(committed) -> None:
    async with committed() as s:
        s.add_all(
            [
                Source(
                    type="media",
                    name="Ok",
                    handle_or_domain="example.ru",
                    feed_url="https://example.ru/rss",
                ),
                Source(
                    type="media",
                    name="Broken",
                    handle_or_domain="broken.ru",
                    feed_url="https://broken.ru/rss",
                ),
            ]
        )
        await s.commit()

    # Та же статья ещё раз в другой рубрике ленты, под другой формой ссылки.
    repeat = (
        b"<item><title>Pozhar</title><link>http://www.example.ru/news/1/</link>"
        b"<description>Other rubric</description></item></channel>"
    )
    routes = {
        "https://example.ru/rss": RSS_FEED.replace(b"</channel>", repeat),
        "https://example.ru/news/1?utm_source=rss": ARTICLE_HTML,
    }
    async with httpx.AsyncClient(transport=_transport(routes)) as client:
        stats = await poll_rss(committed, client=client)
        again = await poll_rss(committed, client=client)

    assert stats["sources"] == 2
    assert stats["failed_sources"] == 1
    assert stats["inserted"] == 2
    assert stats["edited"] == 0  # повтор внутри ленты — не «правка»
    assert again["inserted"] == 0  # повторный проход ничего не дублирует

    async with committed() as s:
        fire = await s.scalar(select(Document).where(Document.url == "https://example.ru/news/1"))
        assert "40 пожарных" in fire.text  # полный текст статьи, а не аннотация
        broken = await s.scalar(select(Source).where(Source.handle_or_domain == "broken.ru"))
        assert broken.consecutive_errors == 2
        assert "500" in broken.last_error


async def test_poll_telegram_reads_channel_preview(committed) -> None:
    async with committed() as s:
        s.add(Source(type="tg", name="Тест", handle_or_domain="@testchan"))
        s.add(Source(type="tg", name="Закрытое превью", handle_or_domain="@nopreview"))
        await s.commit()

    def handler(request: httpx.Request) -> httpx.Response:
        match str(request.url):
            case "https://t.me/s/testchan":
                return httpx.Response(200, content=TELEGRAM_PAGE)
            case "https://t.me/s/nopreview":
                return httpx.Response(302, headers={"Location": "https://t.me/nopreview"})
            case _:
                return httpx.Response(200, content=b"<html>channel card</html>")

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport, follow_redirects=True) as client:
        stats = await poll_telegram(committed, client=client)

    assert stats["inserted"] == 1
    assert stats["failed_sources"] == 1
    async with committed() as s:
        post = await s.scalar(select(Document))
        assert post.url == "https://t.me/testchan/101"
        closed = await s.scalar(select(Source).where(Source.handle_or_domain == "@nopreview"))
        assert "web preview disabled" in closed.last_error


def test_full_text_must_match_title() -> None:
    from app.collectors.rss import text_matches_title

    title = "Росатом построит первую атомную электростанцию на Луне к середине 2030-х годов"
    own = (
        "Россия должна создать первую атомную электростанцию на Луне к середине 2030-х годов, "
        "заявил глава Росатома."
    )
    other = "Владимир Зеленский в телеинтервью в Лондоне рассказал, что после четырёх лет войны"
    assert text_matches_title(title, own)
    assert not text_matches_title(title, other)  # ВЗГЛЯД 15.10.2026: соседняя статья со страницы
    assert text_matches_title("Молния", other)  # слишком короткий заголовок не проверяем
    assert text_matches_title(None, other) and text_matches_title(title, None)


def test_news_sitemap_is_read_like_a_feed() -> None:
    from datetime import UTC, datetime

    from app.collectors.rss import parse_feed

    raw = b"""<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"
        xmlns:news="http://www.google.com/schemas/sitemap-news/0.9">
  <url>
    <loc>https://www.cnn.com/2026/10/15/weather/west-texas-flooding</loc>
    <news:news>
      <news:publication><news:name>CNN</news:name><news:language>en</news:language></news:publication>
      <news:publication_date>2026-10-15T21:04:11.120Z</news:publication_date>
      <news:title>Flash flooding turns West Texas roads to rivers</news:title>
    </news:news>
  </url>
  <url><loc>https://www.cnn.com/2026/10/15/us/no-date</loc></url>
</urlset>"""
    docs = parse_feed(raw, 7)
    assert [d.url for d in docs][0] == "https://www.cnn.com/2026/10/15/weather/west-texas-flooding"
    assert docs[0].title == "Flash flooding turns West Texas roads to rivers"
    assert docs[0].published_at == datetime(2026, 10, 15, 21, 4, 11, 120000, tzinfo=UTC)
    assert docs[0].source_id == 7 and docs[0].text is None
    assert docs[1].title is None and docs[1].published_at is None


def test_unsupported_telegram_post_is_read_from_link_preview() -> None:
    from app.collectors.telegram import og_text, parse_channel_page, unsupported_posts

    page = (
        '<div class="tgme_widget_message text_not_supported_wrap js-widget_message" '
        'data-post="rentv_news/226726"><div class="message_media_not_supported_wrap">'
        '<div class="message_media_not_supported"><div class="message_media_not_supported_label">'
        "Please open Telegram to view this post</div></div></div>"
        '<time datetime="2026-10-19T10:15:55+00:00" class="time">10:15</time></div>'
        '<div class="tgme_widget_message js-widget_message" data-post="rentv_news/226727">'
        '<div class="tgme_widget_message_text">Обычный пост с текстом</div></div>'
    )
    assert [d.url for d in parse_channel_page(page, 1)] == ["https://t.me/rentv_news/226727"]
    [(url, when)] = unsupported_posts(page)
    assert url == "https://t.me/rentv_news/226726" and when.isoformat().startswith(
        "2026-10-19T10:15"
    )
    post_page = (
        '<meta property="og:title" content="РЕН ТВ|Новости">'
        '<meta property="og:description" '
        'content="Влиятельного бизнесмена обвиняют &quot;в насилии&quot;">'
    )
    assert og_text(post_page) == 'Влиятельного бизнесмена обвиняют "в насилии"'
    assert og_text("<html></html>") is None
