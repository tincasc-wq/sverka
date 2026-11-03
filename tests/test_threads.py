"""Threads (threads.com): скачивание поста по ссылке.

Threads — приложение Meta: без JS отдаётся пустая заглушка, серверный рендер с текстом
поста отдаёт только «известным краулерам» (проверено 03.11.2026 на посте @newslabenzo).
"""

import httpx
import pytest
from selectolax.lexbor import LexborHTMLParser as HTMLParser

from app.check import not_an_article
from app.collectors import threads
from app.collectors.urls import normalize_url
from app.models import Source
from tests.fixtures import THREADS_POST_PAGE, THREADS_SHELL_PAGE

POST_URL = "https://www.threads.com/@newslabenzo/post/DfP_J88Dbbz"
SHARE_URL = "https://threads.com/share/_1-KFU35E"
POST_TEXT = (
    "Подписывайся на LABENZO NEWS, чтобы быть в курсе главного: https://t.me/labenzonews/4445"
)


def test_parse_post_extracts_text_and_canonical_url() -> None:
    doc = threads.parse_post(
        THREADS_POST_PAGE,
        f"{POST_URL}?xmt=AQG0EA6sLURjnLA3vgTxqQh5EFbbE2jvfANhT7UhdeUPmAVaIphtt4kUJxK_8t1lvoUWr2U&slof=1",
        source_id=5,
    )

    assert doc is not None
    assert doc.url == "https://threads.com/@newslabenzo/post/DfP_J88Dbbz"
    assert doc.text == POST_TEXT  # HTML-сущности из og:description декодированы
    assert doc.title is None  # og:title — атрибуция автора, не заголовок новости
    assert doc.published_at is None  # Threads не отдаёт время поста серверно
    assert doc.source_id == 5


def test_parse_post_returns_none_for_shell_page() -> None:
    assert threads.parse_post(THREADS_SHELL_PAGE, POST_URL, source_id=None) is None


def test_post_handle_and_share_detection() -> None:
    assert threads.post_handle(f"{POST_URL}?xmt=junk&slof=1") == "@newslabenzo"
    assert threads.post_handle("https://threads.net/@user/post/AbC123") == "@user"
    assert threads.post_handle("https://www.threads.com/@newslabenzo") is None
    assert threads.post_handle("https://threads.com/share/_1-KFU35E") is None

    assert threads.is_share_link(SHARE_URL)
    assert threads.is_share_link("https://www.threads.net/share/AbCdEf")
    assert not threads.is_share_link(POST_URL)

    assert threads.is_threads(POST_URL)
    assert threads.is_threads("https://threads.net/@user/post/AbC123")
    assert not threads.is_threads("https://example.com/@user/post/AbC123")


def test_normalize_url_drops_threads_tracking_params() -> None:
    url = normalize_url(f"{POST_URL}?xmt=AQG0EA6s&slof=1&hl=ru")

    assert url == "https://threads.com/@newslabenzo/post/DfP_J88Dbbz"


def test_profile_link_gets_hint_not_error() -> None:
    reason = not_an_article("https://www.threads.com/@newslabenzo")

    assert reason is not None
    assert "пост" in reason
    assert not_an_article(POST_URL) is None
    assert not_an_article(SHARE_URL) is None


class _ThreadsTransport(httpx.MockTransport):
    """Share-ссылка редиректит на канонический пост (как делает Threads)."""

    def __init__(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            url = str(request.url)
            if url.startswith("https://threads.com/share/"):
                return httpx.Response(
                    301,
                    headers={
                        "Location": f"{POST_URL}"
                        "?xmt=AQG0EA6sLURjnLA3vgTxqQh5EFbbE2jvfANhT7UhdeUPmAVaIphtt4kUJxK_8t1lvoUWr2U&slof=1"
                    },
                )
            if url.split("?")[0] == POST_URL:
                return httpx.Response(200, content=THREADS_POST_PAGE)
            return httpx.Response(404)

        super().__init__(handler)


@pytest.fixture
def threads_client(monkeypatch) -> None:
    def fake_make_client(*args, **kwargs) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=_ThreadsTransport(), follow_redirects=True)

    monkeypatch.setattr(threads, "make_client", fake_make_client)


async def test_resolve_seed_downloads_post_from_share_link(session, threads_client) -> None:
    """Полный путь: шерилка → канонический URL → скачивание краулером → источник по handle."""
    from app.check import resolve_seed

    session.add(Source(type="tg", name="LABENZO NEWS", handle_or_domain="@newslabenzo"))
    await session.commit()

    doc = await resolve_seed(session, SHARE_URL)

    assert doc.url == "https://threads.com/@newslabenzo/post/DfP_J88Dbbz"
    assert doc.text == POST_TEXT
    source = await session.get(Source, doc.source_id)
    assert source is not None
    assert source.handle_or_domain == "@newslabenzo"


def test_crawler_user_agent_is_meta_hit() -> None:
    assert "facebookexternalhit" in threads.CRAWLER_UA
    # Заглушка Threads без краулерского UA: убедимся, что фикстура поста вообще парсится
    tree = HTMLParser(THREADS_POST_PAGE)
    assert tree.css_first('meta[property="og:description"]') is not None
