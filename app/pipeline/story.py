"""Сборка истории вокруг публикации: кандидаты → кластер → граф → запись в БД (6.2–6.3)."""

from dataclasses import dataclass, field
from datetime import timedelta

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.scoring import ScoringConfig, get_scoring
from app.models import Derivation, Document, Source, Story, StoryDocument
from app.pipeline.graph import GraphResult, Node, SourceInfo, build_graph


class SeedNotEmbedded(Exception):
    """У исходной публикации ещё нет эмбеддинга — искать похожие нечем."""


@dataclass
class Candidate:
    document: Document
    similarity: float | None  # косинусная близость к исходной публикации; None — нашли по заголовку
    # core — точно то же событие; probable — та же тема рядом по времени, решит LLM (6.2 п.3).
    membership: str = "core"


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

    distance = Document.embedding.cosine_distance(seed.embedding)
    rows = await session.execute(
        select(Document, (1 - distance).label("sim"))
        .where(Document.id != seed.id, Document.embedding.is_not(None), in_window)
        .order_by(distance)
        .limit(cfg.clustering.candidates_top_k)
    )
    candidates = {doc.id: Candidate(doc, float(sim)) for doc, sim in rows}

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


def _source_info(source: Source) -> SourceInfo:
    return SourceInfo(
        id=source.id,
        name=source.name,
        handle_or_domain=source.handle_or_domain,
        type=source.type,
        owner=source.owner,
        aliases=tuple(source.aliases or ()),
        tracked_since=source.created_at,
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


def is_same_event(seed: Document, candidate: Candidate, cfg: ScoringConfig) -> bool:
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
    # Средняя близость: эмбеддинги не отличают то же событие от соседнего, отметим для LLM.
    candidate.membership = "probable"
    return gap <= timedelta(hours=c.near_hours)


async def build_story(
    session: AsyncSession, seed: Document, cfg: ScoringConfig | None = None
) -> StoryResult:
    cfg = cfg or get_scoring()
    candidates = await find_candidates(session, seed, cfg)
    members = [c for c in candidates if is_same_event(seed, c, cfg)]
    member_ids = {c.document.id for c in members}
    borderline = [
        c
        for c in candidates
        if c.document.id not in member_ids
        and c.similarity is not None
        and c.similarity >= cfg.clustering.llm_review_from
    ]

    all_sources = {s.id: _source_info(s) for s in await session.scalars(select(Source))}
    docs = [seed, *(c.document for c in members)]
    nodes = [to_node(d, all_sources) for d in docs]
    graph = build_graph(nodes, list(all_sources.values()), cfg.graph)

    story = Story(title=seed.title or (seed.text or "")[:200], centroid=seed.embedding)
    session.add(story)
    await session.flush()
    similarity = {c.document.id: c.similarity for c in members}
    session.add_all(
        StoryDocument(story_id=story.id, document_id=d.id, similarity=similarity.get(d.id, 1.0))
        for d in docs
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
