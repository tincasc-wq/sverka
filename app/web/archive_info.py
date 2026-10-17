"""Глубина архива для сайта: с какого дня собираем новости, сколько публикаций и источников.

Считаем раз в 10 минут: цифры меняются медленно, а показываются на каждой странице.
"""

import time
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Document

TTL_SECONDS = 600
_cache: tuple[float, "ArchiveInfo"] | None = None


@dataclass(frozen=True)
class ArchiveInfo:
    since: date | None  # первый день сбора по реестру
    documents: int
    sources: int  # источников, приславших публикации за последние сутки

    @property
    def documents_text(self) -> str:
        """«89 тыс. публикаций», «2,1 млн публикаций», «3 публикации»."""
        from app.web.report import plural

        if self.documents >= 1_000_000:
            return f"{self.documents / 1_000_000:.1f}".replace(".", ",") + " млн публикаций"
        if self.documents >= 10_000:
            return f"{self.documents // 1000} тыс. публикаций"
        return plural(self.documents, "публикация", "публикации", "публикаций")


async def archive_info(session: AsyncSession) -> ArchiveInfo:
    global _cache
    if _cache and time.monotonic() - _cache[0] < TTL_SECONDS:
        return _cache[1]
    from_registry = Document.source_id.is_not(None)
    since = await session.scalar(select(func.min(Document.first_seen_at)).where(from_registry))
    documents = await session.scalar(
        select(func.count()).select_from(Document).where(from_registry)
    )
    sources = await session.scalar(
        select(func.count(func.distinct(Document.source_id))).where(
            from_registry, Document.first_seen_at >= datetime.now(UTC) - timedelta(days=1)
        )
    )
    info = ArchiveInfo(since.date() if since else None, documents or 0, sources or 0)
    _cache = (time.monotonic(), info)
    return info


def reset_cache() -> None:
    global _cache
    _cache = None
