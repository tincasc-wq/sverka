"""Реестр источников в БД: разметка исходов, пересчёт рейтинга, карточка источника (М6)."""

from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.scoring import SourceRatingConfig, get_scoring
from app.models import Document, Outcome, Source, SourceScore, Story, StoryDocument
from app.models.enums import DecidedBy, Stance, StoryStatus, Verdict
from app.registry.rating import Observation, Rating, change_reason, compute_rating


async def set_outcome(
    session: AsyncSession,
    story_id: int,
    verdict: Verdict,
    decided_by: DecidedBy,
    evidence_url: str | None = None,
    stances: dict[int, Stance] | None = None,
) -> Outcome:
    """Исход истории. Каждое решение — новая строка в outcomes, действует последнее."""
    for document_id, stance in (stances or {}).items():
        link = await session.get(StoryDocument, (story_id, document_id))
        if link is None:
            raise ValueError(f"document {document_id} is not in story {story_id}")
        link.analyst_stance = stance
    outcome = Outcome(
        story_id=story_id, verdict=verdict, decided_by=decided_by, evidence_url=evidence_url
    )
    session.add(outcome)
    story = await session.get(Story, story_id)
    story.status = StoryStatus.CLOSED
    await session.flush()
    return outcome


async def latest_outcomes(session: AsyncSession) -> dict[int, Outcome]:
    # Решения только дополняются, id растёт со временем: последнее решение — max(id).
    latest = select(func.max(Outcome.id)).group_by(Outcome.story_id)
    rows = await session.scalars(select(Outcome).where(Outcome.id.in_(latest)))
    return {o.story_id: o for o in rows}


async def observations_by_source(session: AsyncSession) -> dict[int, list[Observation]]:
    """Как каждый источник выступил в историях с известным исходом — одно наблюдение на историю."""
    outcomes = await latest_outcomes(session)
    if not outcomes:
        return {}
    rows = await session.execute(
        select(
            Document.source_id,
            StoryDocument.story_id,
            StoryDocument.membership,
            StoryDocument.analyst_stance,
            Story.title,
        )
        .join(Document, Document.id == StoryDocument.document_id)
        .join(Story, Story.id == StoryDocument.story_id)
        .where(StoryDocument.story_id.in_(outcomes), Document.source_id.is_not(None))
    )
    stances: dict[tuple[int, int], Counter] = defaultdict(Counter)
    titles: dict[int, str | None] = {}
    for source_id, story_id, membership, analyst_stance, title in rows:
        titles[story_id] = title
        if analyst_stance:
            stance = Stance(analyst_stance)
        elif membership == "core":
            stance = Stance.SUPPORTS  # по умолчанию публикация распространяла утверждение
        else:
            continue  # «вероятно то же событие» без решения аналитика не учитываем
        stances[(source_id, story_id)][stance] += 1

    result: dict[int, list[Observation]] = defaultdict(list)
    for (source_id, story_id), counter in stances.items():
        stance = counter.most_common(1)[0][0]
        outcome = outcomes[story_id]
        result[source_id].append(
            Observation(
                story_id=story_id,
                decided_at=outcome.decided_at,
                verdict=Verdict(outcome.verdict),
                stance=stance,
                story_title=titles.get(story_id),
            )
        )
    return result


async def last_scores(session: AsyncSession) -> dict[int, SourceScore]:
    latest = select(func.max(SourceScore.id)).group_by(SourceScore.source_id)
    rows = await session.scalars(select(SourceScore).where(SourceScore.id.in_(latest)))
    return {s.source_id: s for s in rows}


@dataclass
class RecomputeStats:
    sources: int = 0
    changed: int = 0


async def recompute_ratings(
    session: AsyncSession, now: datetime | None = None, cfg: SourceRatingConfig | None = None
) -> RecomputeStats:
    """Ночной пересчёт (6.6). Новая запись в source_scores — только при изменении и с причиной."""
    now = now or datetime.now(UTC)
    cfg = cfg or get_scoring().source_rating
    observations = await observations_by_source(session)
    previous = await last_scores(session)
    stats = RecomputeStats()

    for source in await session.scalars(select(Source).order_by(Source.id)):
        stats.sources += 1
        obs = observations.get(source.id, [])
        rating = compute_rating(source.type, obs, now, cfg)
        last = previous.get(source.id)
        if last is not None:
            unchanged = last.n_observations == rating.n_observations and (
                abs(last.score - rating.score) < cfg.min_score_change
            )
            if unchanged:
                continue
            prev_rating = Rating(last.score, last.ci_low, last.ci_high, last.n_observations, 0, 0)
            new_obs = [o for o in obs if o.decided_at > last.computed_at]
        else:
            prev_rating, new_obs = None, obs
        session.add(
            SourceScore(
                source_id=source.id,
                score=rating.score,
                ci_low=rating.ci_low,
                ci_high=rating.ci_high,
                n_observations=rating.n_observations,
                reason=change_reason(prev_rating, rating, new_obs, source.type, cfg),
                computed_at=now,
            )
        )
        stats.changed += 1
    await session.flush()
    return stats


async def source_card(session: AsyncSession, source: Source) -> dict:
    """Карточка источника для бота, API и админки (ФТ-6.1–6.4)."""
    cfg = get_scoring().source_rating
    history = list(
        await session.scalars(
            select(SourceScore)
            .where(SourceScore.source_id == source.id)
            .order_by(SourceScore.computed_at.desc(), SourceScore.id.desc())
            .limit(20)
        )
    )
    current = history[0] if history else None
    documents = await session.scalar(
        select(func.count()).select_from(Document).where(Document.source_id == source.id)
    )
    return {
        "name": source.name,
        "handle_or_domain": source.handle_or_domain,
        "type": source.type,
        "owner": source.owner,
        "affiliation": source.affiliation,
        "legal_flags": source.legal_flags,
        "documents_in_archive": documents,
        "rating": {
            "score": current.score,
            "ci": [current.ci_low, current.ci_high],
            "outcomes": current.n_observations,
            "shown": current.n_observations >= cfg.min_outcomes_to_show,
            "computed_at": current.computed_at.isoformat(),
        }
        if current
        else None,
        "history": [
            {"at": s.computed_at.isoformat(), "score": s.score, "reason": s.reason} for s in history
        ],
    }


async def find_source(session: AsyncSession, handle: str) -> Source | None:
    """Источник по домену (tass.ru, https://www.tass.ru/) или каналу (@rbc_news, t.me/rbc_news)."""
    handle = handle.strip().lower().removeprefix("https://").removeprefix("http://")
    handle = handle.removeprefix("www.").rstrip("/")
    if handle.startswith("t.me/"):
        handle = "@" + handle.removeprefix("t.me/").split("/")[0]
    elif not handle.startswith("@"):
        handle = handle.split("/")[0]
    return await session.scalar(select(Source).where(Source.handle_or_domain == handle))
