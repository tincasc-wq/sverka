import itertools
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


_axes = itertools.count(1)


def vec(cos: float, axis: int | None = None) -> list[float]:
    """Единичный вектор с близостью cos к оси e0; остаток — по своей оси.

    Разные документы смотрят в разные стороны (как разные события в реальном пространстве),
    поэтому между собой они не похожи — похожи только на e0.
    """
    v = [0.0] * EMBEDDING_DIM
    v[0] = cos
    v[axis or (next(_axes) % (EMBEDDING_DIM - 1) + 1)] = math.sqrt(1 - cos**2)
    return v


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


def unit_mix(*parts: tuple[int, float]) -> list[float]:
    """Единичный вектор из компонент по осям: [(ось, вес), ...]."""
    v = [0.0] * EMBEDDING_DIM
    for axis, weight in parts:
        v[axis] = weight
    norm = math.sqrt(sum(x * x for x in v))
    return [x / norm for x in v]


async def test_opinion_post_still_gets_whole_event_via_core_centroid(session: AsyncSession) -> None:
    # Ось 0 — само событие, ось 1 — авторское мнение, ось 2 — соседняя тема (турпоток).
    seed = Document(
        url="https://t.me/opinion/1",
        text="мнение",
        published_at=T0,
        embedding=unit_mix((0, 1.0), (1, 0.5)),
    )
    session.add(seed)
    await session.flush()
    news = []
    for i in range(3):  # сухие новости о событии: к посту близки (≥0.88), к центру — ещё ближе
        d = Document(
            url=f"https://n{i}.ru/1",
            text="новость",
            published_at=T0,
            embedding=unit_mix((0, 1.0), (1, 0.1 * i)),
        )
        session.add(d)
        news.append(d)
    dry = Document(
        url="https://dry.ru/1",
        text="сухая новость",
        published_at=T0,
        embedding=unit_mix((0, 1.0), (3, 0.45)),
    )  # к посту < 0.88, к центру > 0.88
    other = Document(
        url="https://tour.ru/1",
        text="турпоток",
        published_at=T0,
        embedding=unit_mix((0, 0.55), (2, 1.0)),
    )
    session.add_all([dry, other])
    await session.flush()

    result = await build_story(session, seed)

    membership = {c.document.url: c.membership for c in result.members}
    assert membership["https://dry.ru/1"] == "core"
    assert "https://tour.ru/1" not in membership


async def test_official_short_text_close_in_time_is_core(session: AsyncSession) -> None:
    kremlin = Source(type="official", name="Кремль", handle_or_domain="kremlin.ru")
    session.add(kremlin)
    await session.flush()
    seed = await _doc(session, "https://a.ru/1", 1.0)
    decree = await _doc(session, "https://kremlin.ru/1", 0.83, minutes=-60, source=kremlin)
    media = await _doc(session, "https://b.ru/1", 0.83, minutes=-50)

    result = await build_story(session, seed)

    membership = {c.document.id: c.membership for c in result.members}
    assert membership == {decree.id: "core", media.id: "probable"}


async def test_short_official_seed_gets_core_from_tight_neighbours(session: AsyncSession) -> None:
    kremlin = Source(type="official", name="Кремль", handle_or_domain="kremlin.ru")
    session.add(kremlin)
    await session.flush()
    # Указ: к каждому пересказу ~0.83, а пересказы между собой очень похожи (ось 5).
    decree = Document(
        url="https://kremlin.ru/1",
        text="Указ",
        published_at=T0,
        source_id=kremlin.id,
        embedding=unit_mix((0, 1.0), (5, 0.67)),
    )
    session.add(decree)
    retellings = [
        Document(
            url=f"https://m{i}.ru/1",
            text="пересказ",
            published_at=T0 + timedelta(minutes=50 + i),
            embedding=unit_mix((5, 1.0), (0, 0.55), (20 + i, 0.1)),
        )
        for i in range(4)
    ]
    session.add_all(retellings)
    await session.flush()

    result = await build_story(session, decree, persist=False)

    membership = {c.document.url: c.membership for c in result.members}
    assert all(membership.get(d.url) == "core" for d in retellings)


async def test_loose_topic_neighbours_do_not_form_core(session: AsyncSession) -> None:
    seed = Document(
        url="https://a.ru/1", text="удар", published_at=T0, embedding=unit_mix((0, 1.0), (1, 0.6))
    )
    session.add(seed)
    # Соседи по теме: к посту ~0.83, но каждый про своё (разные оси) — между собой не похожи.
    others = [
        Document(
            url=f"https://o{i}.ru/1",
            text="другое",
            published_at=T0 + timedelta(hours=1),
            embedding=unit_mix((0, 1.0), (30 + i, 0.6)),
        )
        for i in range(4)
    ]
    session.add_all(others)
    await session.flush()

    result = await build_story(session, seed, persist=False)

    assert all(c.membership == "probable" for c in result.members)


async def test_other_language_publication_gets_crosslingual_offset(session: AsyncSession) -> None:
    ria = Source(type="agency", name="РИА", handle_or_domain="ria.ru")
    bbc = Source(type="media", name="BBC", handle_or_domain="bbc.com", lang="en")
    rbc = Source(type="media", name="РБК", handle_or_domain="rbc.ru")
    session.add_all([ria, bbc, rbc])
    await session.flush()
    seed = await _doc(session, "https://ria.ru/1", 1.0, 0, ria)
    english = await _doc(session, "https://bbc.com/1", 0.74, 30, bbc)  # 0.74 + 0.08 = 0.82
    russian = await _doc(session, "https://rbc.ru/1", 0.74, 30, rbc)

    result = await build_story(session, seed)

    assert english.id in {c.document.id for c in result.members}
    assert russian.id not in {c.document.id for c in result.members}


async def test_same_headline_joins_story_despite_low_similarity(session: AsyncSession) -> None:
    from app.pipeline.story import same_headline

    tass = Source(type="agency", name="ТАСС", handle_or_domain="tass.ru")
    rambler = Source(type="aggregator", name="Рамблер", handle_or_domain="news.rambler.test")
    session.add_all([tass, rambler])
    await session.flush()
    headline = "Лихачев: РФ создаст АЭС на Луне к середине следующего 10-летия"
    seed = await _doc(session, "https://t.me/x/1", 1.0, 120, None, "Россия построит АЭС на Луне")
    await _doc(session, "https://news.rambler.test/1", 0.9, 14, rambler, "Текст", title=headline)
    # «Молния» ТАСС: один заголовок, вектор далёк от длинного поста.
    flash = await _doc(session, "https://tass.ru/1", 0.6, 0, tass, None, title=headline)

    # Векторный поиск видит лишь одного ближайшего (как ТАСС вне 50 ближайших на громкой теме):
    # «молния» должна найтись поиском заголовка прямо в базе.
    from app.core.scoring import get_scoring

    cfg = get_scoring().model_copy(deep=True)
    cfg.clustering.candidates_top_k = 1
    result = await build_story(session, seed, cfg)

    assert flash.id in {c.document.id for c in result.members}
    assert same_headline(headline, headline + "!") and not same_headline("Взрыв в Киеве", "Взрыв")


async def test_empty_document_in_archive_is_downloaded_again(session, monkeypatch) -> None:
    from app import check as check_module
    from app.check import resolve_seed
    from app.collectors.store import DocumentIn

    empty = Document(url="https://rosbalt.ru/news/1", title=None, text=None)
    session.add(empty)
    await session.flush()

    async def fake_download(url, source_id):
        return DocumentIn(url=url, title="Атака БПЛА на Петербург", text="Более 120 дронов…")

    monkeypatch.setattr(check_module, "_download", fake_download)
    doc = await resolve_seed(session, "https://rosbalt.ru/news/1?utm_source=yandex.ru")
    assert doc.id == empty.id and doc.title == "Атака БПЛА на Петербург"


async def test_by_name_finds_rare_name_and_skips_common(session) -> None:
    """Поиск по редким именам: редкое имя — кандидат, частое (больше порога) — пропуск."""
    from app.core.scoring import get_scoring
    from app.pipeline.story import find_candidates

    cfg = get_scoring().model_copy(deep=True)
    cfg.clustering.name_max_docs = 2  # «слишком частое имя» — больше двух публикаций окна

    seed = await _doc(
        session,
        "https://example.ru/seed",
        0.99,
        text="Завод Minerva Zen и Минерва Зен остановлены, Трамп уехал.",
    )
    await _doc(
        session, "https://example.ru/rare1", None, minutes=10, text="Minerva Zen снова работает"
    )
    await _doc(
        session, "https://example.ru/rare2", None, minutes=20, text="Минерва Зен отгружает металл"
    )
    await _doc(session, "https://example.ru/c1", None, minutes=30, text="Трамп выступил")
    await _doc(session, "https://example.ru/c2", None, minutes=40, text="Трамп ответил")
    await _doc(session, "https://example.ru/c3", None, minutes=50, text="Трамп уехал")

    candidates = await find_candidates(session, seed, cfg)
    by_name = {c.document.url for c in candidates if c.by_name}

    # «Минерва» встречается редко — публикации найдены по имени (латиница и кириллица).
    assert "https://example.ru/rare1" in by_name
    assert "https://example.ru/rare2" in by_name
    # «Трамп» в трёх публикациях окна (> name_max_docs=2) — пропущен.
    assert not any(u.startswith("https://example.ru/c") for u in by_name)
