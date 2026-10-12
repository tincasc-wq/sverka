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
