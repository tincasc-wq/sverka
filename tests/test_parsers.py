from datetime import UTC, datetime

from app.collectors import rss, telegram
from tests.fixtures import ARTICLE_HTML, RSS_FEED, TELEGRAM_PAGE


def test_parse_feed_extracts_entries() -> None:
    docs = rss.parse_feed(RSS_FEED, source_id=7)

    assert [d.title for d in docs] == [
        "На складе в Подмосковье произошёл пожар",
        "ЦБ сохранил ключевую ставку",
    ]
    assert docs[0].text == "Кратко: горит склад."  # HTML из описания вычищен
    assert docs[0].published_at == datetime(2026, 10, 12, 7, 15, tzinfo=UTC)
    assert all(d.source_id == 7 for d in docs)


def test_extract_article_text_drops_navigation() -> None:
    text = rss.extract_article_text(ARTICLE_HTML, "https://example.ru/news/1")

    assert "40 пожарных" in text
    assert "Главная | Новости" not in text


def test_parse_telegram_page_skips_media_only_posts() -> None:
    docs = telegram.parse_channel_page(TELEGRAM_PAGE, source_id=3)

    assert len(docs) == 1
    assert docs[0].url == "https://t.me/testchan/101"
    assert "Работают 40 пожарных" in docs[0].text
    assert docs[0].published_at == datetime(2026, 10, 12, 7, 20, tzinfo=UTC)


def test_preview_url_accepts_any_handle_form() -> None:
    for handle in ("@rian_ru", "rian_ru", "https://t.me/rian_ru"):
        assert telegram.preview_url(handle) == "https://t.me/s/rian_ru"


def test_published_time_requires_time_not_just_date() -> None:
    from app.check import published_time

    full = '<meta property="article:published_time" content="2026-10-12T15:20:00+03:00">'
    naive = '<meta itemprop="datePublished" content="2026-10-12T15:20">'
    date_only = '<meta property="article:published_time" content="2026-10-12">'

    assert published_time(full) == datetime(2026, 10, 12, 12, 20, tzinfo=UTC)
    assert published_time(naive) == datetime(2026, 10, 12, 12, 20, tzinfo=UTC)
    assert published_time(date_only) is None
    assert published_time("<html></html>") is None


def test_stated_date_and_page_lang() -> None:
    from datetime import date

    from app.check import page_lang, stated_date

    llnl = (
        '<html lang="en"><body><p>On Dec. 5, 2022 and December 5, 2022 the team...</p>'
        '<span class="text-muted">March 23, 2023</span></body></html>'
    )
    meta = '<meta property="article:published_time" content="2025-01-31">'
    ru = "<html><body><p>Опубликовано 5 октября 2026, обновлено 7 октября 2026</p></body></html>"
    future = "<body>Конференция пройдёт 1 декабря 2099 года</body>"

    assert stated_date(llnl) == date(2023, 3, 23)  # самая поздняя из дат на странице
    assert stated_date(meta) == date(2025, 1, 31)
    assert stated_date(ru) == date(2026, 10, 7)
    assert stated_date(future) is None and stated_date("<html></html>") is None
    assert page_lang(llnl) == "en" and page_lang(ru) == "ru"
    assert page_lang('<html lang="de-DE">') == "de"
