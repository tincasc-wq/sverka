"""Калибровка кластеризации для пар на разных языках (англоязычная публикация ↔ русская).

  python -m app.quality.crosslingual [--tass N] [--other N] [--json]

Для каждой англоязычной публикации берём ближайшие русские в окне ±12 ч, разметку «то же
событие» делает LLM-судья (тот же шаг same_event, что в конвейере). Дальше — распределение
близости для совпавших и несовпавших пар и доля совпавших, которые вообще попадают в top-K
поиска кандидатов (их могут вытеснить соседи на своём языке).
Нужен ключ LLM; стоимость — около 0.07 ₽ на публикацию.
"""

import argparse
import asyncio
import json
import random
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import SessionLocal
from app.core.scoring import get_scoring
from app.llm import LlmClient
from app.models import Document, Source
from app.pipeline.llm_steps import judge_same_event

NEIGHBOURS = 5
WINDOW = timedelta(hours=12)
THRESHOLDS = (0.70, 0.72, 0.74, 0.76, 0.78, 0.80, 0.82, 0.84, 0.86, 0.88)


def _time(doc: Document):
    return doc.published_at or doc.first_seen_at


async def _sample(session: AsyncSession, tass: int, other: int, seed: int) -> list[Document]:
    base = (
        select(Document)
        .join(Source, Source.id == Document.source_id)
        .where(Source.lang == "en", Document.embedding.is_not(None))
    )
    tass_docs = list(await session.scalars(base.where(Source.handle_or_domain == "tass.com")))
    others = list(await session.scalars(base.where(Source.handle_or_domain != "tass.com")))
    rng = random.Random(seed)
    return rng.sample(tass_docs, min(tass, len(tass_docs))) + rng.sample(
        others, min(other, len(others))
    )


async def _pairs(session: AsyncSession, doc: Document, top_k: int) -> dict:
    t = _time(doc)
    distance = Document.embedding.cosine_distance(doc.embedding)
    in_window = (Document.id != doc.id) & Document.embedding.is_not(None)
    when = Document.published_at.is_(None) | Document.published_at.between(t - WINDOW, t + WINDOW)
    ru = list(
        await session.execute(
            select(Document, 1 - distance)
            .join(Source, Source.id == Document.source_id)
            .where(in_window, when, Source.lang == "ru")
            .order_by(distance)
            .limit(NEIGHBOURS)
        )
    )
    # Порог top-K как в поиске кандидатов: с какой близости начинается «вытеснение».
    kth = await session.scalar(
        select(1 - distance).where(in_window, when).order_by(distance).offset(top_k - 1).limit(1)
    )
    return {"ru": [(d, float(s)) for d, s in ru], "kth": float(kth) if kth is not None else 0.0}


async def run(tass: int, other: int, seed: int = 1) -> dict:
    cfg = get_scoring().clustering
    client = LlmClient()
    async with SessionLocal() as session:
        docs = await _sample(session, tass, other, seed)
        rows = []
        for doc in docs:
            p = await _pairs(session, doc, cfg.candidates_top_k)
            if not p["ru"]:
                continue
            decisions = await judge_same_event(client, session, doc, [d for d, _ in p["ru"]])
            source = await session.get(Source, doc.source_id)
            for rank, (cand, sim) in enumerate(p["ru"], 1):
                d = decisions.get(cand.id)
                if d is None:
                    continue
                rows.append(
                    {
                        "en_id": doc.id,
                        "ru_id": cand.id,
                        "tass": source.handle_or_domain == "tass.com",
                        "sim": round(sim, 4),
                        "rank": rank,
                        "in_top_k": sim >= p["kth"],
                        "same": d.same_event,
                        "reason": d.reason,
                        "en_title": doc.title,
                        "ru_title": cand.title,
                    }
                )
        await session.commit()  # учёт расходов в llm_calls
    return summarize(rows, cfg.strong_threshold, cfg.include_threshold) | {"pairs": rows}


def summarize(rows: list[dict], strong: float, include: float) -> dict:
    def stats(subset: list[dict]) -> dict:
        pos = [r["sim"] for r in subset if r["same"]]
        neg = [r["sim"] for r in subset if not r["same"]]
        table = []
        for t in THRESHOLDS:
            tp = sum(s >= t for s in pos)
            fp = sum(s >= t for s in neg)
            table.append(
                {
                    "threshold": t,
                    "recall": round(tp / len(pos), 2) if pos else None,
                    "precision": round(tp / (tp + fp), 2) if tp + fp else None,
                }
            )
        return {
            "pairs": len(subset),
            "same": len(pos),
            "same_sim": _quantiles(pos),
            "other_sim": _quantiles(neg),
            "same_in_top_k": round(sum(r["in_top_k"] for r in subset if r["same"]) / len(pos), 2)
            if pos
            else None,
            f"same_ge_{include}": round(sum(s >= include for s in pos) / len(pos), 2)
            if pos
            else None,
            f"same_ge_{strong}": round(sum(s >= strong for s in pos) / len(pos), 2)
            if pos
            else None,
            "by_threshold": table,
        }

    return {
        "tass": stats([r for r in rows if r["tass"]]),
        "other": stats([r for r in rows if not r["tass"]]),
    }


def _quantiles(values: list[float]) -> dict | None:
    if not values:
        return None
    v = sorted(values)

    def q(p: float) -> float:
        return round(v[min(len(v) - 1, int(p * len(v)))], 3)

    return {"min": v[0], "p10": q(0.1), "p50": q(0.5), "p90": q(0.9), "max": v[-1]}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--tass", type=int, default=107)
    parser.add_argument("--other", type=int, default=80)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    result = asyncio.run(run(args.tass, args.other))
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=1, default=str))
        return
    for name in ("tass", "other"):
        print(f"== {name}")
        print(json.dumps(result[name], ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
