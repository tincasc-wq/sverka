from datetime import UTC, datetime

import pytest
from sqlalchemy import inspect, select, text, update
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Base, Document, Outcome, Source, SourceScore, Story
from app.models.base import EMBEDDING_DIM


async def test_migration_creates_every_model_table(session: AsyncSession) -> None:
    conn = await session.connection()
    tables = await conn.run_sync(lambda c: set(inspect(c).get_table_names()))

    assert set(Base.metadata.tables) <= tables


async def test_extensions_installed(session: AsyncSession) -> None:
    rows = await session.execute(text("SELECT extname FROM pg_extension"))

    assert {"vector", "pg_trgm"} <= set(rows.scalars())


async def test_first_seen_at_cannot_change(session: AsyncSession) -> None:
    doc = Document(url="https://example.org/a", title="Заголовок")
    session.add(doc)
    await session.flush()

    # Остальные поля менять можно.
    await session.execute(update(Document).where(Document.id == doc.id).values(title="Новый"))

    with pytest.raises(DBAPIError, match="first_seen_at is immutable"):
        await session.execute(
            update(Document)
            .where(Document.id == doc.id)
            .values(first_seen_at=datetime(2020, 1, 1, tzinfo=UTC))
        )


async def _source(session: AsyncSession) -> Source:
    source = Source(type="media", name="Тестовое СМИ", handle_or_domain="example.org")
    session.add(source)
    await session.flush()
    return source


async def test_source_scores_are_append_only(session: AsyncSession) -> None:
    source = await _source(session)
    score = SourceScore(
        source_id=source.id, score=50, ci_low=30, ci_high=70, n_observations=0, reason="старт"
    )
    session.add(score)
    await session.flush()

    with pytest.raises(DBAPIError, match="source_scores is append-only"):
        await session.execute(update(SourceScore).values(score=99))


async def test_outcomes_cannot_be_deleted(session: AsyncSession) -> None:
    story = Story(title="Событие")
    session.add(story)
    await session.flush()
    session.add(Outcome(story_id=story.id, verdict="confirmed", decided_by="analyst"))
    await session.flush()

    with pytest.raises(DBAPIError, match="outcomes is append-only"):
        await session.execute(text("DELETE FROM outcomes"))


async def test_unknown_source_type_rejected(session: AsyncSession) -> None:
    session.add(Source(type="blog", name="Блог", handle_or_domain="blog.example"))

    with pytest.raises(IntegrityError, match="ck_sources_type"):
        await session.flush()


async def test_vector_search_orders_by_cosine_distance(session: AsyncSession) -> None:
    near = [1.0] + [0.0] * (EMBEDDING_DIM - 1)
    far = [0.0, 1.0] + [0.0] * (EMBEDDING_DIM - 2)
    session.add_all(
        [
            Document(url="https://example.org/near", embedding=near),
            Document(url="https://example.org/far", embedding=far),
        ]
    )
    await session.flush()

    query = [0.9, 0.1] + [0.0] * (EMBEDDING_DIM - 2)
    rows = await session.execute(
        select(Document.url).order_by(Document.embedding.cosine_distance(query))
    )

    assert rows.scalars().all() == ["https://example.org/near", "https://example.org/far"]
