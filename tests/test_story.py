import json
import math
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.check import run_check
from app.collectors.http import UnsafeUrl, fetch, make_client
from app.models import Check, Derivation, Document, Source, StoryDocument
from app.models.base import EMBEDDING_DIM
from app.pipeline.story import SeedNotEmbedded, build_story
from tests.test_embeddings import FakeEmbedder

T0 = datetime(2026, 10, 12, 9, 0, tzinfo=UTC)


def vec(cos: float) -> list[float]:
    """Единичный вектор с заданной косинусной близостью к оси e0."""
    return [cos, math.sqrt(1 - cos**2)] + [0.0] * (EMBEDDING_DIM - 2)


async def _doc(session, url, cos, minutes=0, source=None, text="Текст", title=None):
    d = Document(
        url=url,
        title=title,
        text=text,
        embedding=vec(cos) if cos is not None else None,
        published_at=T0 + timedelta(minutes=minutes),
        source_id=source.id if source else None,
    )
    session.add(d)
    await session.flush()
    return d


async def test_story_takes_close_documents_and_marks_borderline(session: AsyncSession) -> None:
    tass = Source(type="agency", name="ТАСС", handle_or_domain="tass.ru", aliases=["ТАСС"])
    mash = Source(type="tg", name="Mash", handle_or_domain="@breakingmash")
    session.add_all([tass, mash])
    await session.flush()

    seed = await _doc(
        session,
        "https://t.me/breakingmash/1",
        1.0,
        30,
        mash,
        text="Как сообщает ТАСС, в Подольске горит склад.",
    )
    original = await _doc(session, "https://tass.ru/1", 0.93, 0, tass, text="Горит склад.")
    border = await _doc(session, "https://e.ru/border", 0.78)
    await _doc(session, "https://e.ru/far", 0.1)
    await _doc(session, "https://e.ru/old", 0.99, minutes=-60 * 24 * 5)  # вне окна ±72 ч

    result = await build_story(session, seed)

    assert {c.document.id for c in result.members} == {original.id}
    assert [c.document.id for c in result.borderline] == [border.id]
    assert result.graph.primary_id == original.id
    members = await session.scalars(
        select(StoryDocument.document_id).where(StoryDocument.story_id == result.story_id)
    )
    assert set(members) == {seed.id, original.id}
    edge = await session.scalar(select(Derivation))
    assert (edge.from_doc_id, edge.to_doc_id, edge.kind) == (original.id, seed.id, "attribution")


async def test_fresh_document_without_embedding_found_by_title(session: AsyncSession) -> None:
    seed = await _doc(session, "https://a.ru/1", 1.0, title="Пожар на складе в Подольске")
    fresh = await _doc(
        session, "https://b.ru/1", None, 5, title="Пожар на складе в Подольске потушен"
    )

    result = await build_story(session, seed)

    assert [c.document.id for c in result.members] == [fresh.id]


async def test_seed_without_embedding_is_rejected(session: AsyncSession) -> None:
    seed = await _doc(session, "https://a.ru/1", None)

    with pytest.raises(SeedNotEmbedded):
        await build_story(session, seed)


async def test_run_check_embeds_seed_and_records_check(session: AsyncSession) -> None:
    seed = await _doc(session, "https://a.ru/1", None, text="Горит склад")

    check = await run_check(session, f"doc:{seed.id}", FakeEmbedder())
    report = json.loads(check.summary)

    assert report["publications"] == 1
    assert report["primary"]["url"] == "https://a.ru/1"
    assert check.status == "done"
    assert len(check.public_token) >= 16
    assert await session.scalar(select(Check)) is check


async def test_recent_check_is_reused_from_cache(session: AsyncSession) -> None:
    seed = await _doc(session, "https://a.ru/1", 1.0)

    first = await run_check(session, f"doc:{seed.id}", None, cache_hours=6)
    again = await run_check(session, f"doc:{seed.id}", None, cache_hours=6)
    fresh = await run_check(session, f"doc:{seed.id}", None, cache_hours=0)

    assert again.id == first.id
    assert fresh.id != first.id


@pytest.mark.parametrize(
    "url", ["http://127.0.0.1:8000/health", "http://10.0.0.1/", "http://db:5432/"]
)
async def test_user_urls_to_internal_addresses_are_blocked(url: str) -> None:
    async with make_client(public_only=True) as client:
        with pytest.raises(UnsafeUrl):
            await fetch(client, url)


async def test_medium_similarity_counts_only_close_in_time(session: AsyncSession) -> None:
    seed = await _doc(session, "https://a.ru/seed", 1.0, minutes=0)
    near = await _doc(session, "https://b.ru/near", 0.84, minutes=90)
    far = await _doc(
        session, "https://c.ru/far", 0.84, minutes=60 * 30
    )  # другой день — другое событие
    strong_far = await _doc(session, "https://d.ru/strong", 0.92, minutes=60 * 30)

    result = await build_story(session, seed)

    assert {c.document.id: c.membership for c in result.members} == {
        near.id: "probable",
        strong_far.id: "core",
    }
    assert [c.document.id for c in result.borderline] == [far.id]


async def test_recheck_of_same_publication_reuses_story(session: AsyncSession) -> None:
    seed = await _doc(session, "https://a.ru/seed", 1.0)
    await _doc(session, "https://b.ru/1", 0.95, minutes=10)
    first = await build_story(session, seed)
    late = await _doc(
        session, "https://c.ru/late", 0.93, minutes=60
    )  # пришла после первой проверки

    second = await build_story(session, seed)

    assert second.story_id == first.story_id
    members = await session.scalars(
        select(StoryDocument.document_id).where(StoryDocument.story_id == first.story_id)
    )
    assert late.id in set(members)
