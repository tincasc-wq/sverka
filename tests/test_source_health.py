from datetime import UTC, datetime, timedelta

from app.collectors.health import classify, digest
from app.models import Source

NOW = datetime(2026, 10, 20, 9, 0, tzinfo=UTC)


def src(**kw) -> Source:
    base = dict(id=1, name="Канал", type="tg", handle_or_domain="@c", is_tracked=True, feed_url="f")
    base.update(kw)
    s = Source(**base)
    s.consecutive_errors = kw.get("consecutive_errors", 0)
    s.last_polled_at = kw.get("last_polled_at", NOW)
    return s


def row(last_hours: float, docs_14d=140, docs_3d=30, full_3d=20, before=110, full_before=80):
    return {
        "last_doc": NOW - timedelta(hours=last_hours),
        "docs_14d": docs_14d,
        "docs_3d": docs_3d,
        "full_3d": full_3d,
        "docs_before": before,
        "full_before": full_before,
    }


def test_errors_and_off():
    assert classify(src(is_tracked=False), row(1), NOW).status == "off"
    assert classify(src(consecutive_errors=2), row(1), NOW).status == "ok"
    h = classify(src(consecutive_errors=1543, last_error="502"), row(1), NOW)
    assert h.status == "red" and "1543" in h.reason


def test_silence_relative_to_usual_pace():
    # Обычно 10 публикаций в день: молчание неделю — поломка (URA.RU (TG), 12.10.2026).
    h = classify(src(), row(24 * 7), NOW)
    assert h.status == "red" and "молчит 7 дн." in h.reason
    assert classify(src(), row(20), NOW).status == "ok"  # меньше суток — не тревожим
    # Редкий источник (МАГАТЭ: 3 публикации за две недели) — четыре дня тишины в норме.
    rare = row(24 * 4, docs_14d=3, docs_3d=0, full_3d=0, before=3, full_before=3)
    assert classify(src(type="official"), rare, NOW).status == "ok"
    assert classify(src(), None, NOW).status == "red"  # опрашивали, но ничего не пришло
    assert classify(src(type="max"), None, NOW).status == "ok"  # MAX не собирается


def test_full_text_drop():
    # Le Monde 12–16.10.2026: вместо статей — заглушки, полного текста почти не стало.
    h = classify(src(type="media"), row(1, full_3d=2), NOW)
    assert h.status == "yellow" and "полного текста" in h.reason
    # ТАСС: всегда только аннотации — это не падение.
    tass = row(1, full_3d=0, full_before=0)
    assert classify(src(type="agency"), tass, NOW).status == "ok"


def test_digest_reports_changes_only():
    a, b = src(id=1, name="WaPo"), src(id=2, name="Phys.org")
    health = {1: classify(a, row(24 * 6), NOW), 2: classify(b, row(1), NOW)}
    text = digest([a, b], health, before={2})
    assert "Сломались" in text and "WaPo" in text and "Починились" in text
    assert digest([a, b], health, before={1}) is None


def test_source_without_feed_is_not_silent():
    # The Washington Post: ленты нет, источник только в реестре — молчание не поломка.
    assert classify(src(type="media", feed_url=None), row(24 * 6), NOW).status == "ok"


def test_new_rare_source_and_registry_only_source_are_not_alarms():
    fresh = src(type="official")
    fresh.created_at = NOW - timedelta(days=2)
    h = classify(fresh, None, NOW)
    assert h.status == "ok" and "свежих записей" in h.reason  # ВОЗ, EMA: добавлены 20.10.2026
    old = src(type="official")
    old.created_at = NOW - timedelta(days=30)
    assert classify(old, None, NOW).status == "red"
    # The Washington Post: ленту убрали, а старый счётчик ошибок остался.
    wapo = src(type="media", feed_url=None, consecutive_errors=1547, last_error="502")
    assert classify(wapo, row(24 * 8), NOW).status == "ok"
