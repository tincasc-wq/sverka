from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Document, Source, SourceScore, Story, StoryDocument
from app.models.enums import DecidedBy, Stance, Verdict
from app.registry.service import recompute_ratings, set_outcome

NOW = datetime(2026, 10, 12, 12, tzinfo=UTC)


async def _story(session, *members) -> Story:
    """members: (source, membership)."""
    story = Story(title="Пожар на складе")
    session.add(story)
    await session.flush()
    for i, (source, membership) in enumerate(members):
        doc = Document(url=f"https://{source.handle_or_domain}/{story.id}-{i}", source_id=source.id)
        session.add(doc)
        await session.flush()
        session.add(StoryDocument(story_id=story.id, document_id=doc.id, membership=membership))
    await session.flush()
    return story


async def _source(session, handle, type_="media") -> Source:
    s = Source(type=type_, name=handle, handle_or_domain=handle)
    session.add(s)
    await session.flush()
    return s


async def _scores(session, source) -> list[SourceScore]:
    rows = await session.scalars(
        select(SourceScore).where(SourceScore.source_id == source.id).order_by(SourceScore.id)
    )
    return list(rows)


async def test_first_recompute_writes_prior_with_reason(session: AsyncSession) -> None:
    src = await _source(session, "a.ru")

    stats = await recompute_ratings(session, NOW)

    [score] = await _scores(session, src)
    assert stats.changed == 1
    assert score.score == 60.0
    assert "Стартовая оценка" in score.reason


async def test_unchanged_rating_is_not_written_again(session: AsyncSession) -> None:
    src = await _source(session, "a.ru")
    await recompute_ratings(session, NOW)

    stats = await recompute_ratings(session, NOW + timedelta(hours=1))

    assert stats.changed == 0
    assert len(await _scores(session, src)) == 1


async def test_refuted_story_lowers_spreader_and_raises_debunker(session: AsyncSession) -> None:
    spreader = await _source(session, "fake.ru")
    debunker = await _source(session, "check.ru")
    bystander = await _source(session, "other.ru")
    story = await _story(session, (spreader, "core"), (debunker, "core"), (bystander, "probable"))
    await recompute_ratings(session, NOW - timedelta(days=1))
    debunker_doc = await session.scalar(
        select(Document.id).where(Document.source_id == debunker.id)
    )

    await set_outcome(
        session,
        story.id,
        Verdict.REFUTED,
        DecidedBy.ANALYST,
        evidence_url="https://mchs.gov.ru/opr",
        stances={debunker_doc: Stance.CONTRADICTS},
    )
    await recompute_ratings(session, NOW)

    spreader_scores = await _scores(session, spreader)
    debunker_scores = await _scores(session, debunker)
    assert spreader_scores[-1].score < spreader_scores[0].score
    assert debunker_scores[-1].score > debunker_scores[0].score
    assert f"история #{story.id}" in spreader_scores[-1].reason
    # «Вероятно то же событие» без решения аналитика в рейтинг не идёт.
    assert len(await _scores(session, bystander)) == 1
    assert (await session.get(Story, story.id)).status == "closed"


async def test_latest_outcome_wins(session: AsyncSession) -> None:
    src = await _source(session, "a.ru")
    story = await _story(session, (src, "core"))
    await set_outcome(session, story.id, Verdict.REFUTED, DecidedBy.AUTO)
    await set_outcome(session, story.id, Verdict.CONFIRMED, DecidedBy.ANALYST)

    await recompute_ratings(session, NOW)

    [score] = await _scores(session, src)
    assert score.score > 60.0
    assert "подтвердилась" in score.reason


async def test_stance_for_foreign_document_is_rejected(session: AsyncSession) -> None:
    src = await _source(session, "a.ru")
    story = await _story(session, (src, "core"))

    with pytest.raises(ValueError):
        await set_outcome(
            session,
            story.id,
            Verdict.CONFIRMED,
            DecidedBy.ANALYST,
            stances={999999: Stance.SUPPORTS},
        )
