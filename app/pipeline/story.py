"""Сборка истории вокруг публикации: кандидаты → кластер → граф → запись в БД (6.2–6.3)."""

import math
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import timedelta

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.scoring import ScoringConfig, get_scoring
from app.models import Derivation, Document, Source, Story, StoryDocument
from app.pipeline.graph import GraphResult, Node, SourceInfo, build_graph
from app.registry.legal import legal_note


class SeedNotEmbedded(Exception):
    """У исходной публикации ещё нет эмбеддинга — искать похожие нечем."""


@dataclass
class Candidate:
    document: Document
    similarity: float | None  # косинусная близость к исходной публикации; None — нашли по заголовку
    # core — точно то же событие; probable — та же тема рядом по времени, решит LLM (6.2 п.3).
    membership: str = "core"
    llm_reason: str | None = None  # объяснение LLM, если решение «то же событие?» принимала она


@dataclass
class StoryResult:
    story_id: int
    seed: Document
    members: list[Candidate]
    borderline: list[Candidate]
    nodes: list[Node]
    graph: GraphResult
    sources: dict[int, SourceInfo] = field(default_factory=dict)


def _event_time(doc: Document):
    return func.coalesce(doc.published_at, doc.first_seen_at)


async def find_candidates(
    session: AsyncSession, seed: Document, cfg: ScoringConfig
) -> list[Candidate]:
    if seed.embedding is None:
        raise SeedNotEmbedded(f"document {seed.id} has no embedding yet")
    center = seed.published_at or seed.first_seen_at
    window = timedelta(hours=cfg.clustering.window_hours)
    in_window = _event_time(Document).between(center - window, center + window)

    async def nearest(vector) -> dict[int, Document]:
        distance = Document.embedding.cosine_distance(vector)
        rows = await session.scalars(
            select(Document)
            .where(Document.id != seed.id, Document.embedding.is_not(None), in_window)
            .order_by(distance)
            .limit(cfg.clustering.candidates_top_k)
        )
        return {doc.id: doc for doc in rows}

    offset = cfg.clustering.crosslingual_offset
    seed_lang = await _lang_of(session, seed.source_id)

    async def shifted(docs: dict[int, Document], raw: dict[int, float]) -> dict[int, float]:
        # Разные языки — близость ниже при том же событии; выравниваем по калибровке.
        langs = await _langs(session, {d.source_id for d in docs.values()})
        return {
            i: min(1.0, s + offset) if langs.get(docs[i].source_id, "ru") != seed_lang else s
            for i, s in raw.items()
        }

    seed_vec = _unit(seed.embedding)
    found = await nearest(seed.embedding)
    sims = await shifted(found, {i: _dot(seed_vec, _unit(d.embedding)) for i, d in found.items()})

    # Второй проход — от центра ядра. Иначе история зависит от формулировки присланного
    # поста: у поста-мнения («скорее всего продлят…») близость к сухим новостям о том же
    # событии ниже порога, и половина ядра уходит в «другие события» (проверено 12.10.2026).
    core = [d for i, d in found.items() if sims[i] >= cfg.clustering.strong_threshold]
    if not core:
        core = _tight_neighbours(seed, found, sims, cfg)
    if core:
        vectors = [seed_vec, *(_unit(d.embedding) for d in core)]
        centroid = _unit([sum(xs) for xs in zip(*vectors, strict=True)])
        found.update(await nearest(centroid))
        raw = {}
        for i, d in found.items():
            vec = _unit(d.embedding)
            raw[i] = max(_dot(seed_vec, vec), _dot(centroid, vec))
        sims = await shifted(found, raw)
    candidates = {i: Candidate(d, sims[i]) for i, d in found.items()}

    # Свежие публикации без эмбеддинга ещё не попали в векторный поиск — ловим по заголовку.
    if seed.title:
        title_sim = func.similarity(Document.title, seed.title)
        rows = await session.scalars(
            select(Document).where(
                Document.id != seed.id,
                Document.embedding.is_(None),
                in_window,
                title_sim >= cfg.clustering.title_trgm_threshold,
            )
        )
        for doc in rows:
            candidates.setdefault(doc.id, Candidate(doc, None))
    return list(candidates.values())


async def _langs(session: AsyncSession, source_ids: set[int | None]) -> dict[int, str]:
    ids = {i for i in source_ids if i is not None}
    if not ids:
        return {}
    rows = await session.execute(select(Source.id, Source.lang).where(Source.id.in_(ids)))
    return {source_id: lang for source_id, lang in rows}


async def _lang_of(session: AsyncSession, source_id: int | None) -> str:
    return (await _langs(session, {source_id})).get(source_id, "ru")


def _tight_neighbours(
    seed: Document, found: dict[int, Document], sims: dict[int, float], cfg: ScoringConfig
) -> list[Document]:
    """Ядро для короткого сухого текста (указ, сообщение ведомства), у которого своего ядра нет.

    Его близость к пересказам ~0.83 — ниже порога, и без этого история с него разваливалась
    до 4 публикаций из ~30 (проверка устойчивости 12.10.2026). Берём ближайших соседей рядом
    по времени, но только если они плотно похожи друг на друга — значит, это одно событие.
    У «соседей по теме» (разные удары, реакции) взаимная близость ниже, и они не проходят.
    """
    c = cfg.clustering
    seed_time = seed.published_at or seed.first_seen_at
    near = [
        found[i]
        for i in sorted(found, key=lambda i: -sims[i])
        if sims[i] >= c.include_threshold
        and abs((found[i].published_at or found[i].first_seen_at) - seed_time)
        <= timedelta(hours=c.near_hours)
    ][:5]
    if len(near) < 3:
        return []
    vectors = [_unit(d.embedding) for d in near]
    pairs = [(a, b) for i, a in enumerate(vectors) for b in vectors[i + 1 :]]
    mutual = sum(_dot(a, b) for a, b in pairs) / len(pairs)
    return near if mutual >= c.strong_threshold else []


# Решатель «то же событие?»: (seed, кандидаты) → {id документа: (да/нет, объяснение)}.
EventJudge = Callable[[Document, list[Document]], Awaitable[dict[int, tuple[bool, str]]]]
MAX_JUDGED = 24


async def _apply_judge(
    judge: EventJudge,
    seed: Document,
    members: list[Candidate],
    borderline: list[Candidate],
    cfg: ScoringConfig,
) -> tuple[list[Candidate], list[Candidate]]:
    """LLM решает пограничные случаи (6.2 п.3): «вероятно то же событие» и близкие кандидаты.

    Эмбеддинги в полосе 0.80–0.88 не отличают то же событие от соседнего (проверено на
    историях про хуситов и «Теорию победы»), поэтому здесь решает модель. При сбое LLM всё
    остаётся как было — с пометкой «вероятно».
    """
    doubtful = [c for c in members if c.membership == "probable"] + borderline
    doubtful.sort(key=lambda c: -(c.similarity or 0))
    doubtful = doubtful[:MAX_JUDGED]
    if not doubtful:
        return members, borderline
    try:
        decisions = await judge(seed, [c.document for c in doubtful])
    except Exception:  # noqa: BLE001 — без LLM остаётся детерминированный результат
        return members, borderline
    judged = {c.document.id: c for c in doubtful}
    kept = [c for c in members if c.document.id not in judged]
    rest = [c for c in borderline if c.document.id not in judged]
    for doc_id, c in judged.items():
        if doc_id not in decisions:
            (kept if c in members else rest).append(c)
            continue
        same, reason = decisions[doc_id]
        c.llm_reason = reason
        if same:
            c.membership = "core"
            kept.append(c)
        else:
            rest.append(c)
    return kept, rest


def _unit(vector) -> list[float]:
    values = [float(x) for x in vector]
    norm = math.sqrt(sum(x * x for x in values)) or 1.0
    return [x / norm for x in values]


def _dot(a: list[float], b: list[float]) -> float:
    return sum(x * y for x, y in zip(a, b, strict=True))


def _source_info(source: Source) -> SourceInfo:
    return SourceInfo(
        id=source.id,
        name=source.name,
        handle_or_domain=source.handle_or_domain,
        type=source.type,
        owner=source.owner,
        aliases=tuple(source.aliases or ()),
        tracked_since=source.created_at,
        legal_note=legal_note(source.legal_flags),
    )


def to_node(doc: Document, sources: dict[int, SourceInfo]) -> Node:
    return Node(
        id=doc.id,
        url=doc.url,
        observed_at=doc.first_seen_at,
        claimed_at=doc.published_at,
        source=sources.get(doc.source_id) if doc.source_id else None,
        title=doc.title,
        text=doc.text,
        links=tuple(doc.links or ()),
        simhash=doc.simhash,
    )


def is_same_event(
    seed: Document, candidate: Candidate, cfg: ScoringConfig, official: bool = False
) -> bool:
    """Одно событие, а не просто одна тема (6.2).

    Перепечатки одного события у bge-m3 дают близость ~0.88+; «та же тема, другое событие»
    (новый удар, реакция МИД) — 0.82–0.86. Короткие тексты (указ, пост в канале) того же
    события тоже бывают ~0.83, поэтому средняя близость засчитывается только рядом по времени.
    """
    c = cfg.clustering
    if candidate.similarity is None or candidate.similarity >= c.strong_threshold:
        candidate.membership = "core"  # почти совпадающий заголовок или высокая близость
        return True
    if candidate.similarity < c.include_threshold:
        return False
    seed_time = seed.published_at or seed.first_seen_at
    doc = candidate.document
    gap = abs((doc.published_at or doc.first_seen_at) - seed_time)
    if gap > timedelta(hours=c.near_hours):
        return False
    # Официальный текст (указ, сообщение ведомства) короткий и сухой, его близость к
    # пересказам систематически ниже: рядом по времени он — ядро события, а не «вероятно».
    # Средняя близость остального: эмбеддинги не отличают то же событие от соседнего — для LLM.
    candidate.membership = "core" if official else "probable"
    return True


async def build_story(
    session: AsyncSession,
    seed: Document,
    cfg: ScoringConfig | None = None,
    *,
    persist: bool = True,
    judge: "EventJudge | None" = None,
) -> StoryResult:
    """persist=False — только посчитать (проверка устойчивости), без записи в БД."""
    cfg = cfg or get_scoring()
    candidates = await find_candidates(session, seed, cfg)
    all_sources = {s.id: _source_info(s) for s in await session.scalars(select(Source))}

    def is_official(c: Candidate) -> bool:
        source = all_sources.get(c.document.source_id)
        return bool(source and source.type == "official")

    members = [c for c in candidates if is_same_event(seed, c, cfg, is_official(c))]
    member_ids = {c.document.id for c in members}
    borderline = [
        c
        for c in candidates
        if c.document.id not in member_ids
        and c.similarity is not None
        and c.similarity >= cfg.clustering.llm_review_from
    ]
    if judge is not None:
        members, borderline = await _apply_judge(judge, seed, members, borderline, cfg)

    docs = [seed, *(c.document for c in members)]
    nodes = [to_node(d, all_sources) for d in docs]
    graph = build_graph(nodes, list(all_sources.values()), cfg.graph)
    if not persist:
        return StoryResult(0, seed, members, borderline, nodes, graph, all_sources)

    # Повторная проверка той же публикации дополняет её историю, а не плодит новую:
    # иначе одно событие висит в очереди аналитика несколько раз и разметка дробится.
    story = await session.scalar(
        select(Story)
        .join(StoryDocument, StoryDocument.story_id == Story.id)
        .where(StoryDocument.document_id == seed.id)
        .order_by(Story.id.desc())
        .limit(1)
    )
    if story is None:
        story = Story(title=seed.title or (seed.text or "")[:200], centroid=seed.embedding)
        session.add(story)
        await session.flush()
    by_id = {c.document.id: c for c in members}
    for d in docs:
        await session.execute(
            insert(StoryDocument)
            .values(
                story_id=story.id,
                document_id=d.id,
                similarity=by_id[d.id].similarity if d.id in by_id else 1.0,
                membership=by_id[d.id].membership if d.id in by_id else "core",
            )
            # Повторная проверка уточняет близость и уровень; решение аналитика не трогаем.
            .on_conflict_do_update(
                index_elements=["story_id", "document_id"],
                set_={
                    "similarity": by_id[d.id].similarity if d.id in by_id else 1.0,
                    "membership": by_id[d.id].membership if d.id in by_id else "core",
                },
            )
        )
    for edge in graph.edges:
        await session.execute(
            insert(Derivation)
            .values(
                from_doc_id=edge.from_id,
                to_doc_id=edge.to_id,
                kind=edge.kind.value,
                confidence=edge.confidence,
            )
            .on_conflict_do_nothing(constraint="uq_derivations_edge")
        )
    await session.flush()
    return StoryResult(
        story_id=story.id,
        seed=seed,
        members=members,
        borderline=borderline,
        nodes=nodes,
        graph=graph,
        sources=all_sources,
    )
