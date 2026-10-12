"""LLM-анализ истории: утверждения → сверка → второй контур → индекс (ТЗ 6.4–6.5, раздел 13).

Результат пишется в claims / claim_evidence и возвращается секцией отчёта.
"""

from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.scoring import ScoringConfig
from app.llm import LlmClient
from app.models import Claim, ClaimEvidence, Document, LlmCall, SourceScore
from app.pipeline.graph import Node
from app.pipeline.index import Evidence, score_news
from app.pipeline.llm_steps import extract_claims, judge_same_event, verify_many
from app.pipeline.story import EventJudge, StoryResult

USD_RUB = 90  # для отображения расходов в отчёте; тарифы провайдеров — в долларах

STATUS_RU = {
    "widely_confirmed": "широко подтверждено",
    "partially_confirmed": "частично подтверждено",
    "not_confirmed": "не подтверждено",
    "refuted": "есть опровержения",
    "too_early": "слишком рано: независимых сообщений ещё нет",
    "no_checkable_claims": "нет проверяемых утверждений",
}


def make_judge(client: LlmClient, session: AsyncSession, check_id: int) -> EventJudge:
    async def judge(seed: Document, docs: list[Document]) -> dict[int, tuple[bool, str]]:
        decisions = await judge_same_event(client, session, seed, docs, check_id=check_id)
        return {doc_id: (d.same_event, d.reason) for doc_id, d in decisions.items()}

    return judge


def documents_to_verify(result: StoryResult, limit: int) -> list[tuple[Node, bool]]:
    """По одной публикации на группу владения (кроме группы самой новости).

    Сначала независимые группы (корни графа), затем перепечатки — они подтверждением не
    считаются, но расхождения в них (другие цифры) тоже важны.
    """
    core = {result.seed.id} | {c.document.id for c in result.members if c.membership == "core"}
    has_incoming = {e.to_id for e in result.graph.edges}
    seed_node = next(n for n in result.nodes if n.id == result.seed.id)
    first_by_group: dict[str, Node] = {}
    for n in sorted(result.nodes, key=lambda n: n.observed_at):
        if n.id in core and n.group != seed_node.group:
            first_by_group.setdefault(n.group, n)
    picked = [(n, n.id not in has_incoming) for n in first_by_group.values()]
    picked.sort(key=lambda p: (not p[1], p[0].observed_at))
    return picked[:limit]


async def latest_ratings(session: AsyncSession, source_ids: set[int]) -> dict[int, float]:
    if not source_ids:
        return {}
    latest = (
        select(func.max(SourceScore.id))
        .where(SourceScore.source_id.in_(source_ids))
        .group_by(SourceScore.source_id)
    )
    rows = await session.scalars(select(SourceScore).where(SourceScore.id.in_(latest)))
    return {s.source_id: s.score for s in rows}


async def analyze(
    session: AsyncSession,
    client: LlmClient,
    result: StoryResult,
    check_id: int,
    cfg: ScoringConfig,
) -> dict:
    seed = result.seed
    nodes = {n.id: n for n in result.nodes}
    claims_out = await extract_claims(client, session, seed, check_id=check_id)
    claim_rows = [
        Claim(
            check_id=check_id,
            text=c.text,
            structured=c.model_dump(exclude={"text", "checkable"}),
            is_checkable=c.checkable,
        )
        for c in claims_out
    ]
    session.add_all(claim_rows)
    await session.flush()
    texts = [c.text for c in claims_out]

    targets = documents_to_verify(result, cfg.confirmation_index.max_verify_documents)
    docs = {
        d.id: d for d in [c.document for c in result.members] if d.id in {n.id for n, _ in targets}
    }
    verified = (
        await verify_many(client, session, texts, list(docs.values()), check_id=check_id)
        if texts
        else {}
    )
    rejected = sum(r for _, r in verified.values())

    # Второй контур (раздел 13): противоречия перепроверяет другая модель с рассуждениями.
    disputed_docs = [
        docs[i] for i, (res, _) in verified.items() if any(r.stance == "contradicts" for r in res)
    ]
    second = (
        await verify_many(
            client, session, texts, disputed_docs, step="verify_disputed", check_id=check_id
        )
        if disputed_docs
        else {}
    )

    ratings = await latest_ratings(session, {n.source.id for n, _ in targets if n.source})
    primary = nodes.get(result.graph.primary_id) if result.graph.primary_id else None
    rating_node = primary if primary and primary.source else nodes[seed.id]
    primary_rating = ratings.get(rating_node.source.id) if rating_node.source else None
    if rating_node.source and primary_rating is None:
        primary_rating = (await latest_ratings(session, {rating_node.source.id})).get(
            rating_node.source.id
        )

    independent = {n.id: ind for n, ind in targets}
    evidence: list[list[Evidence]] = [[] for _ in texts]
    report_claims = [
        {"text": c.text, "attributed_to": c.attributed_to, "checkable": c.checkable, "evidence": []}
        for c in claims_out
    ]
    low_confidence = 0
    for doc_id, (results, _) in verified.items():
        node = nodes[doc_id]
        checked = {r.claim: r for r in second.get(doc_id, ([], 0))[0]}
        for r in results:
            stance, flag = r.stance, False
            if r.stance == "contradicts" and doc_id in second:
                again = checked.get(r.claim)
                if again is None or again.stance != "contradicts":
                    # Модели разошлись — в индекс не идёт, показываем как «низкая уверенность».
                    stance, flag = "unrelated", True
                    low_confidence += 1
            idx = r.claim - 1
            session.add(
                ClaimEvidence(
                    claim_id=claim_rows[idx].id,
                    document_id=doc_id,
                    stance=stance,
                    quote=r.quote,
                    discrepancy={"text": r.discrepancy, "low_confidence": flag}
                    if r.discrepancy or flag
                    else None,
                    is_independent=independent[doc_id],
                )
            )
            if stance == "unrelated" and not flag:
                continue
            evidence[idx].append(
                Evidence(
                    group=node.group,
                    source_type=node.source.type if node.source else "media",
                    rating=ratings.get(node.source.id) if node.source else None,
                    stance=stance,
                    independent=independent[doc_id],
                )
            )
            report_claims[idx]["evidence"].append(
                {
                    "source": node.source.name if node.source else node.group,
                    "url": node.url,
                    "stance": r.stance if flag else stance,
                    "quote": r.quote,
                    "discrepancy": r.discrepancy,
                    "independent": independent[doc_id],
                    "low_confidence": flag,
                }
            )

    seed_time = seed.published_at or seed.first_seen_at
    hours = (datetime.now(UTC) - seed_time).total_seconds() / 3600
    news = score_news(
        evidence, [c.checkable for c in claims_out], primary_rating, hours, cfg.confirmation_index
    )
    for rc, score in zip(report_claims, news.claims, strict=True):
        rc["index"] = score.index
        rc["status"] = score.status
        rc["independent_groups"] = len(score.independent_groups)

    cost = await session.scalar(
        select(func.coalesce(func.sum(LlmCall.cost_usd), 0)).where(LlmCall.check_id == check_id)
    )
    await session.flush()
    return {
        "index": news.index,
        "status": news.status,
        "status_text": STATUS_RU.get(news.status, news.status),
        "claims": report_claims,
        "verified_documents": len(verified),
        "rejected_quotes": rejected,
        "low_confidence": low_confidence,
        "cost_rub": round(float(cost) * USD_RUB, 2),
    }
