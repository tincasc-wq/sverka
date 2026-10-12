from datetime import timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Document, Source, Story, StoryDocument
from app.quality.consistency import SeedRun, check_consistency, jaccard, summarize
from tests.test_story import T0, unit_mix


def run(seed_id, primary, core, source="s"):
    return SeedRun(seed_id, source, f"https://x/{seed_id}", primary, True, core)


def test_jaccard() -> None:
    assert jaccard({1, 2, 3}, {2, 3, 4}) == 0.5
    assert jaccard(set(), set()) == 1.0


def test_summary_flags_seed_with_other_primary_and_small_core() -> None:
    runs = [
        run(1, "https://kremlin.ru/1", [1, 2, 3, 4]),
        run(2, "https://kremlin.ru/1", [1, 2, 3, 4]),
        run(3, "https://rbc.ru/1", [3, 9]),  # пост-мнение: другой первоисточник, мало ядра
    ]

    report = summarize("story:1", runs)

    assert report.primary_agreement == round(2 / 3, 3)
    assert report.modal_primary == "https://kremlin.ru/1"
    assert report.consensus_size == 4
    assert [r.seed_id for r in report.divergent] == [3]


async def test_same_event_from_different_sources_agrees(session: AsyncSession) -> None:
    story = Story(title="Событие")
    session.add(story)
    await session.flush()
    docs = []
    for i, handle in enumerate(["a.ru", "b.ru", "c.ru", "d.ru"]):
        src = Source(type="media", name=handle, handle_or_domain=handle)
        session.add(src)
        await session.flush()
        # Пересказы одного события: общая ось 0 и немного своего стиля.
        doc = Document(
            url=f"https://{handle}/1",
            text="текст",
            source_id=src.id,
            published_at=T0 + timedelta(minutes=10 * i),
            embedding=unit_mix((0, 1.0), (10 + i, 0.25)),
        )
        session.add(doc)
        docs.append(doc)
    await session.flush()
    session.add_all(StoryDocument(story_id=story.id, document_id=d.id) for d in docs)
    await session.flush()

    report = await check_consistency(session, f"story:{story.id}")

    assert report.seeds == 4
    assert report.primary_agreement == 1.0
    assert report.modal_primary == "https://a.ru/1"
    assert report.mean_pairwise_jaccard == 1.0
    assert report.divergent == []
