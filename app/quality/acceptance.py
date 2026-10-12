"""Прогон качества LLM-проверки (приёмка этапа 3).

  python -m app.quality.acceptance [--n 25] [--hours 36] [--seed 1] > result.json

Берёт N свежих публикаций разных типов (по одной на событие), проверяет каждую дважды
и пишет JSON: время, цена, индекс и статус обоих прогонов, отброшенные цитаты,
противоречия и выборку подтверждений — для ручной проверки.
"""

import argparse
import asyncio
import json
import math
import random
import time
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select

from app.check import run_check
from app.core.db import SessionLocal
from app.llm import LlmClient
from app.models import Document, Source

STRATA = {  # сколько публикаций каждого вида берём (доля от N)
    "agency": 0.2,
    "media": 0.28,
    "tg": 0.2,
    "official": 0.12,
    "en": 0.2,
}
SAME_EVENT_SIM = 0.8  # ближе этого к уже выбранной — то же событие, пропускаем


def _cos(a, b) -> float:
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    return dot / (math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b)))


async def pick_seeds(n: int, hours: float, seed: int) -> list[tuple[int, str]]:
    rng = random.Random(seed)
    since = datetime.now(UTC) - timedelta(hours=hours)
    picked: list[tuple[Document, str]] = []
    async with SessionLocal() as session:
        for kind, share in STRATA.items():
            query = (
                select(Document)
                .join(Source, Source.id == Document.source_id)
                .where(
                    Document.embedding.is_not(None),
                    # По времени публикации: первый сбор подтягивает из лент и старые записи.
                    func.coalesce(Document.published_at, Document.first_seen_at) >= since,
                    func.length(Document.text) >= 300,
                )
            )
            if kind == "en":
                query = query.where(Source.lang == "en")
            else:
                query = query.where(Source.lang == "ru", Source.type == kind)
            pool = list(await session.scalars(query))
            rng.shuffle(pool)
            want = max(1, round(n * share))
            taken = 0
            for doc in pool:
                if taken >= want:
                    break
                vec = [float(x) for x in doc.embedding]
                if any(
                    _cos(vec, [float(x) for x in d.embedding]) >= SAME_EVENT_SIM for d, _ in picked
                ):
                    continue
                picked.append((doc, kind))
                taken += 1
    return [(d.id, kind) for d, kind in picked]


def _digest(report: dict) -> dict:
    a = report.get("analysis") or {}
    contradictions, supports = [], []
    for c in a.get("claims", []):
        for e in c["evidence"]:
            item = {
                "claim": c["text"],
                "source": e["source"],
                "quote": e["quote"],
                "discrepancy": e.get("discrepancy"),
                "low_confidence": e["low_confidence"],
                "independent": e["independent"],
            }
            if e["stance"] == "contradicts":
                contradictions.append(item)
            elif e["stance"] in ("supports", "refines"):
                supports.append(item)
    return {
        "index": a.get("index"),
        "status": a.get("status"),
        "error": a.get("error"),
        "claims": [c["text"] for c in a.get("claims", [])],
        "checkable": sum(c["checkable"] for c in a.get("claims", [])),
        "publications": report.get("publications"),
        "verified": a.get("verified_documents"),
        "rejected_quotes": a.get("rejected_quotes"),
        "low_confidence": a.get("low_confidence"),
        "cost_rub": a.get("cost_rub"),
        "contradictions": contradictions,
        "supports": supports,
    }


async def run(n: int, hours: float, seed: int) -> dict:
    seeds = await pick_seeds(n, hours, seed)
    llm = LlmClient()
    rows = []
    for doc_id, kind in seeds:
        runs = []
        for _ in (1, 2):
            started = time.monotonic()
            async with SessionLocal() as session:
                try:
                    check = await run_check(session, f"doc:{doc_id}", None, llm=llm)
                    digest = _digest(json.loads(check.summary))
                except Exception as exc:  # noqa: BLE001 — прогон продолжается
                    digest = {"error": f"{type(exc).__name__}: {exc}"[:300]}
            digest["seconds"] = round(time.monotonic() - started, 1)
            runs.append(digest)
        async with SessionLocal() as session:
            doc = await session.get(Document, doc_id)
            source = await session.get(Source, doc.source_id)
        rows.append(
            {
                "doc_id": doc_id,
                "kind": kind,
                "source": source.name,
                "title": (doc.title or doc.text or "")[:120],
                "runs": runs,
            }
        )
    return {"rows": rows, "summary": summarize(rows)}


def summarize(rows: list[dict]) -> dict:
    first = [r["runs"][0] for r in rows]
    ok = [r for r in first if not r.get("error")]
    both = [
        (a["index"], b["index"])
        for a, b in (r["runs"] for r in rows)
        if a.get("index") is not None and b.get("index") is not None
    ]
    same_status = sum(a.get("status") == b.get("status") for a, b in (r["runs"] for r in rows))
    secs = sorted(r["seconds"] for r in first)
    costs = [r["cost_rub"] for r in ok if r.get("cost_rub") is not None]
    statuses: dict[str, int] = {}
    for r in ok:
        statuses[r["status"]] = statuses.get(r["status"], 0) + 1
    return {
        "news": len(rows),
        "errors": len(first) - len(ok),
        "statuses": statuses,
        "status_stable": f"{same_status}/{len(rows)}",
        "index_abs_diff_mean": round(sum(abs(a - b) for a, b in both) / len(both), 1)
        if both
        else None,
        "index_abs_diff_max": max((abs(a - b) for a, b in both), default=None),
        "seconds_p50": secs[len(secs) // 2] if secs else None,
        "seconds_max": secs[-1] if secs else None,
        "cost_rub_mean": round(sum(costs) / len(costs), 2) if costs else None,
        "cost_rub_total_two_runs": round(
            sum(x.get("cost_rub") or 0 for r in rows for x in r["runs"]), 2
        ),
        "contradictions": sum(len(r["contradictions"]) for r in ok),
        "contradictions_low_confidence": sum(
            sum(c["low_confidence"] for c in r["contradictions"]) for r in ok
        ),
        "rejected_quotes": sum(r["rejected_quotes"] or 0 for r in ok),
        "evidence_items": sum(len(r["supports"]) + len(r["contradictions"]) for r in ok),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--n", type=int, default=25)
    parser.add_argument("--hours", type=float, default=36)
    parser.add_argument("--seed", type=int, default=1)
    args = parser.parse_args()
    result = asyncio.run(run(args.n, args.hours, args.seed))
    print(json.dumps(result, ensure_ascii=False, indent=1, default=str))


if __name__ == "__main__":
    main()
