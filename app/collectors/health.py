"""Живость источников: не только ошибки сборщика, но и тихие поломки.

Ошибки подряд (consecutive_errors) видны и так. Опаснее поломки без ошибок: канал забросили,
а опрос проходит (URA.RU, «Кремлёвский пул РИА», «Полит.Ру» молчали с 12.10.2026, ошибок —
ноль), или вместо статей приходят заглушки (Le Monde 12–16.10.2026). Поэтому для каждого
источника смотрим ещё на молчание относительно его обычного темпа и на долю полного текста.
"""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from html import escape

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Source

ERRORS_RED = 3  # ошибок подряд: один-два таймаута — не поломка
SILENT_MIN = timedelta(hours=24)  # молчание короче суток не тревожим
SILENT_FACTOR = 4  # «молчит» — дольше четырёх обычных промежутков между публикациями
FULL_TEXT_CHARS = 500  # публикация «с текстом» — не короче этого
THIN_MIN_DOCS = 10  # долю текста оцениваем, только если публикаций достаточно
NOT_COLLECTED = {"max"}  # MAX не собирается (заглушка), его молчание — не поломка
NEW_SOURCE_GRACE = timedelta(days=14)  # новому источнику без публикаций даём две недели

STATS = text(f"""
select source_id,
  max(first_seen_at) as last_doc,
  count(*) filter (where first_seen_at > now() - interval '14 days') as docs_14d,
  count(*) filter (where first_seen_at > now() - interval '3 days') as docs_3d,
  count(*) filter (where first_seen_at > now() - interval '3 days'
                   and length(coalesce(text, '')) >= {FULL_TEXT_CHARS}) as full_3d,
  count(*) filter (where first_seen_at <= now() - interval '3 days'
                   and first_seen_at > now() - interval '14 days') as docs_before,
  count(*) filter (where first_seen_at <= now() - interval '3 days'
                   and first_seen_at > now() - interval '14 days'
                   and length(coalesce(text, '')) >= {FULL_TEXT_CHARS}) as full_before
from documents
where source_id is not null
group by source_id
""")


@dataclass
class Health:
    status: str  # off | red | yellow | ok
    reason: str
    last_doc: datetime | None = None
    full_share: float | None = None  # доля публикаций с полным текстом за 3 дня

    @property
    def label(self) -> str:
        return {"off": "выключен", "red": "сломан", "yellow": "внимание", "ok": "ок"}[self.status]


def _ago(delta: timedelta) -> str:
    hours = int(delta.total_seconds() // 3600)
    return f"{hours // 24} дн." if hours >= 48 else f"{hours} ч"


def classify(source: Source, row: dict | None, now: datetime) -> Health:
    row = row or {}
    last_doc = row.get("last_doc")
    docs_3d, full_3d = row.get("docs_3d", 0), row.get("full_3d", 0)
    share = full_3d / docs_3d if docs_3d else None
    health = Health("ok", "", last_doc, share)
    if not source.is_tracked:
        health.status, health.reason = "off", "сбор выключен"
        return health
    if source.type in NOT_COLLECTED or (source.type != "tg" and not source.feed_url):
        # Не собирается, только в реестре. Старый счётчик ошибок не в счёт: у The Washington
        # Post ленту убрали, а 1547 ошибок с прошлых опросов остались (22.10.2026).
        return health
    if source.consecutive_errors >= ERRORS_RED:
        health.status = "red"
        health.reason = f"ошибок подряд: {source.consecutive_errors} ({source.last_error or '—'})"
        return health
    if last_doc is None:
        created = source.created_at
        if created is not None and now - created < NEW_SOURCE_GRACE:
            # Новый редкий источник: записи старше трёх дней сборщик не берёт, а свежих ещё не
            # было (ВОЗ, EMA — добавлены 20.10.2026, ложная тревога в сводке).
            health.reason = "свежих записей ещё не было"
        elif source.last_polled_at is not None:
            health.status, health.reason = "red", "ни одной публикации"
        return health
    docs_14d = row.get("docs_14d", 0)
    usual_gap = timedelta(days=14) / docs_14d if docs_14d >= 3 else timedelta(days=3)
    limit = max(SILENT_MIN, usual_gap * SILENT_FACTOR)
    silence = now - last_doc
    if silence > limit:
        health.status = "red"
        health.reason = f"молчит {_ago(silence)} (обычно публикация раз в {_ago(usual_gap)})"
        return health
    before, full_before = row.get("docs_before", 0), row.get("full_before", 0)
    if docs_3d >= THIN_MIN_DOCS and before >= THIN_MIN_DOCS:
        was = full_before / before
        if was >= 0.3 and share is not None and share < was / 2:
            health.status = "yellow"
            health.reason = f"полного текста стало меньше: {share:.0%} вместо {was:.0%}"
    return health


async def source_health(session: AsyncSession, sources: list[Source]) -> dict[int, Health]:
    rows = {r.source_id: dict(r._mapping) for r in await session.execute(STATS)}
    now = datetime.now(UTC)
    return {s.id: classify(s, rows.get(s.id), now) for s in sources}


def digest(sources: list[Source], health: dict[int, Health], before: set[int]) -> str | None:
    """Сводка администратору: что сломалось и что починилось с прошлой сводки."""
    broken = {s.id for s in sources if health[s.id].status == "red"}
    names = {s.id: s.name for s in sources}
    new = sorted(broken - before, key=names.get)
    fixed = sorted((before - broken) & names.keys(), key=names.get)
    if not new and not fixed:
        return None
    lines = ["<b>Источники «Сверки»</b>"]
    if new:
        lines.append("\nСломались:")
        lines += [f"• {escape(names[i])} — {escape(health[i].reason)}" for i in new]
    if fixed:
        lines.append("\nПочинились:")
        lines += [f"• {escape(names[i])}" for i in fixed]
    lines.append(f"\nВсего сломанных: {len(broken)}. Подробно: /admin/sources")
    return "\n".join(lines)
