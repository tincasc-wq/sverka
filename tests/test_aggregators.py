"""Агрегаторы: не независимое подтверждение и не «первоисточник» без оговорки."""

import json

from sqlalchemy.ext.asyncio import AsyncSession

from app.bot import texts
from app.check import run_check
from app.collectors.seeds import read_seeds
from app.models import Source
from app.registry.legal import source_note
from tests.test_embeddings import FakeEmbedder
from tests.test_story import _doc


async def _sources(session: AsyncSession):
    agg = Source(type="aggregator", name="Новости Mail", handle_or_domain="news.mail.test")
    media = Source(type="media", name="Газета", handle_or_domain="gazeta.test")
    other = Source(type="agency", name="Агентство", handle_or_domain="agency.test")
    session.add_all([agg, media, other])
    await session.flush()
    return agg, media, other


async def test_aggregator_first_is_flagged_not_called_primary(session: AsyncSession) -> None:
    agg, media, _ = await _sources(session)
    await _doc(session, "https://news.mail.test/1", 0.95, 0, agg, "Пожар на складе")
    seed = await _doc(
        session, "https://gazeta.test/1", 1.0, 60, media, "Пожар на складе в Подольске"
    )

    report = json.loads((await run_check(session, f"doc:{seed.id}", FakeEmbedder())).summary)

    assert report["primary"]["aggregator"] is True
    assert any("агрегатор" in n for n in report["primary"]["notes"])
    text = texts.format_check(report, None, "http://x/r/1")
    assert "Раньше всех в архиве — агрегатор" in text and "Первоисточник:" not in text


async def test_aggregator_never_counts_as_independent(session: AsyncSession) -> None:
    agg, media, other = await _sources(session)
    seed = await _doc(session, "https://gazeta.test/2", 1.0, 0, media, "Новость")
    await _doc(session, "https://news.mail.test/2", 0.95, 30, agg, "Пересказ новости")

    report = json.loads((await run_check(session, f"doc:{seed.id}", FakeEmbedder())).summary)
    assert report["publications"] == 2 and report["independent_reports"] == 1
    assert report["primary"]["aggregator"] is False

    await _doc(session, "https://agency.test/2", 0.94, 40, other, "Своё сообщение агентства")
    again = json.loads((await run_check(session, f"doc:{seed.id}", FakeEmbedder())).summary)
    assert again["independent_reports"] == 2  # агентство засчитано, агрегатор — нет


def test_registry_marks_aggregators() -> None:
    from pathlib import Path

    seeds = {s.handle_or_domain: s for s in read_seeds(Path("seeds/sources.yaml"))}
    for handle in ("news.rambler.ru", "news.mail.ru", "@bbbreaking", "news.yahoo.com"):
        assert seeds[handle].type == "aggregator", handle
    assert source_note("aggregator", None) == "агрегатор"
    assert source_note("aggregator", {"blocked_in_ru": "x"}) == "агрегатор, заблокировано в РФ"
