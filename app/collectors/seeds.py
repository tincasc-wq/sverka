"""Загрузка стартового реестра из seeds/sources.yaml (upsert по handle_or_domain)."""

from pathlib import Path

import yaml
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Source
from app.models.enums import SourceType


class SeedSource(BaseModel):
    name: str
    type: SourceType
    handle_or_domain: str
    rss: str | None = None
    owner: str | None = None
    affiliation: str | None = None
    aliases: list[str] = []
    is_tracked: bool = True
    lang: str = "ru"
    via_gateway: bool = False
    poll_minutes: int | None = None
    legal_flags: dict[str, str] = {}


def read_seeds(path: Path) -> list[SeedSource]:
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return [SeedSource.model_validate(item) for item in data.get("sources") or []]


async def load_seeds(session: AsyncSession, seeds: list[SeedSource]) -> tuple[int, int]:
    """Возвращает (добавлено, обновлено). Рейтинги и состояние сборщика не трогает."""
    added = updated = 0
    for seed in seeds:
        source = await session.scalar(
            select(Source).where(Source.handle_or_domain == seed.handle_or_domain)
        )
        if source is None:
            source = Source(handle_or_domain=seed.handle_or_domain, is_tracked=seed.is_tracked)
            session.add(source)
            added += 1
        else:
            updated += 1
            if not seed.is_tracked:
                # Включённый в реестре сбор не перебивает выключение галочкой в админке.
                source.is_tracked = False
        source.name = seed.name
        source.type = seed.type
        source.feed_url = seed.rss
        source.owner = seed.owner
        source.affiliation = seed.affiliation
        source.aliases = seed.aliases
        source.lang = seed.lang
        source.via_gateway = seed.via_gateway
        source.poll_minutes = seed.poll_minutes
        source.legal_flags = seed.legal_flags
    await session.commit()
    return added, updated
