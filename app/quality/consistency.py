"""Проверка устойчивости (ТЗ, раздел 13): одно событие с разных входов должно давать один ответ.

Берём публикации одного события от разных источников (ссылка СМИ, пост-мнение, канал) и
запускаем от каждой сборку истории без записи в БД. Сравниваем первоисточник и состав ядра.

    python -m app.quality.consistency story:<id> [--seeds N] [--json]
    python -m app.quality.consistency doc:<id>   [--seeds N] [--json]
"""

import asyncio
import json
import sys
from collections import Counter
from dataclasses import asdict, dataclass, field
from itertools import combinations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import SessionLocal
from app.models import Document, Source, StoryDocument
from app.pipeline.story import StoryResult, build_story


@dataclass
class SeedRun:
    seed_id: int
    seed_source: str
    seed_url: str
    primary_url: str | None
    primary_core: bool  # первоисточник из ядра, а не из «вероятно то же событие»
    core: list[int]
    jaccard_to_consensus: float = 0.0


@dataclass
class ConsistencyReport:
    event: str
    seeds: int
    primary_agreement: float  # доля запусков с самым частым первоисточником
    modal_primary: str | None
    mean_pairwise_jaccard: float  # среднее сходство состава ядра между запусками
    consensus_size: int  # публикаций, которые попали в ядро хотя бы в половине запусков
    runs: list[SeedRun] = field(default_factory=list)
    divergent: list[SeedRun] = field(default_factory=list)


def jaccard(a: set[int], b: set[int]) -> float:
    return len(a & b) / len(a | b) if a | b else 1.0


def _run(result: StoryResult, source_name: str) -> SeedRun:
    membership = {c.document.id: c.membership for c in result.members}
    core = {result.seed.id} | {i for i, m in membership.items() if m == "core"}
    nodes = {n.id: n for n in result.nodes}
    primary = nodes.get(result.graph.primary_id) if result.graph.primary_id else None
    return SeedRun(
        seed_id=result.seed.id,
        seed_source=source_name,
        seed_url=result.seed.url,
        primary_url=primary.url if primary else None,
        primary_core=bool(
            primary and (primary.id == result.seed.id or membership.get(primary.id) == "core")
        ),
        core=sorted(core),
    )


def summarize(event: str, runs: list[SeedRun], min_jaccard: float = 0.7) -> ConsistencyReport:
    primaries = Counter(r.primary_url for r in runs)
    modal, modal_count = primaries.most_common(1)[0] if runs else (None, 0)
    counts = Counter(i for r in runs for i in r.core)
    consensus = {i for i, n in counts.items() if n >= len(runs) / 2}
    for r in runs:
        r.jaccard_to_consensus = round(jaccard(set(r.core), consensus), 3)
    pairs = list(combinations(runs, 2))
    mean_jaccard = (
        sum(jaccard(set(a.core), set(b.core)) for a, b in pairs) / len(pairs) if pairs else 1.0
    )
    divergent = [r for r in runs if r.primary_url != modal or r.jaccard_to_consensus < min_jaccard]
    return ConsistencyReport(
        event=event,
        seeds=len(runs),
        primary_agreement=round(modal_count / len(runs), 3) if runs else 1.0,
        modal_primary=modal,
        mean_pairwise_jaccard=round(mean_jaccard, 3),
        consensus_size=len(consensus),
        runs=runs,
        divergent=divergent,
    )


async def _event_documents(session: AsyncSession, ref: str) -> list[Document]:
    """Публикации события: по истории из БД или по ядру, собранному от одной публикации."""
    kind, _, value = ref.partition(":")
    if kind == "story":
        rows = await session.scalars(
            select(Document)
            .join(StoryDocument, StoryDocument.document_id == Document.id)
            .where(StoryDocument.story_id == int(value), StoryDocument.membership == "core")
        )
        return list(rows)
    if kind == "doc":
        seed = await session.get(Document, int(value))
        if seed is None:
            raise ValueError(f"no document {ref}")
        result = await build_story(session, seed, persist=False)
        core = [c.document for c in result.members if c.membership == "core"]
        return [seed, *core]
    raise ValueError("ожидается story:<id> или doc:<id>")


def _unknown_name(url: str) -> str:
    parts = url.split("/")
    return "@" + parts[3] if parts[2] == "t.me" else parts[2]


def pick_seeds(docs: list[Document], sources: dict[int, Source], limit: int) -> list[Document]:
    """По одной публикации на группу владения — разные редакции, разные формулировки."""
    seen: set[str] = set()
    picked: list[Document] = []
    for doc in sorted(docs, key=lambda d: d.id):
        source = sources.get(doc.source_id)
        group = (source.owner or source.handle_or_domain) if source else doc.url.split("/")[2]
        if doc.embedding is None or group in seen:
            continue
        seen.add(group)
        picked.append(doc)
        if len(picked) >= limit:
            break
    return picked


async def check_consistency(
    session: AsyncSession, ref: str, max_seeds: int = 10
) -> ConsistencyReport:
    sources = {s.id: s for s in await session.scalars(select(Source))}
    docs = await _event_documents(session, ref)
    runs = []
    for seed in pick_seeds(docs, sources, max_seeds):
        result = await build_story(session, seed, persist=False)
        source = sources.get(seed.source_id)
        runs.append(_run(result, source.name if source else _unknown_name(seed.url)))
    return summarize(ref, runs)


def format_report(report: ConsistencyReport) -> str:
    lines = [
        f"Событие {report.event}: запусков {report.seeds}",
        f"  согласие по первоисточнику: {report.primary_agreement:.0%} ({report.modal_primary})",
        f"  среднее сходство ядра между запусками (Jaccard): {report.mean_pairwise_jaccard:.2f}",
        f"  устойчивое ядро: {report.consensus_size} публикаций",
        "",
    ]
    for r in report.runs:
        mark = "  ⚠" if r in report.divergent else "   "
        primary = (r.primary_url or "—") + ("" if r.primary_core else " [вероятно]")
        lines.append(
            f"{mark} {r.seed_source[:24]:24} ядро {len(r.core):3}  "
            f"сходство {r.jaccard_to_consensus:.2f}  первоисточник {primary}"
        )
    return "\n".join(lines)


async def _main(args: list[str]) -> None:
    ref = args[0]
    max_seeds = int(args[args.index("--seeds") + 1]) if "--seeds" in args else 10
    async with SessionLocal() as session:
        report = await check_consistency(session, ref, max_seeds)
    if "--json" in args:
        print(json.dumps(asdict(report), ensure_ascii=False, indent=2))
    else:
        print(format_report(report))


if __name__ == "__main__":
    if not sys.argv[1:]:
        print(__doc__)
        sys.exit(1)
    asyncio.run(_main(sys.argv[1:]))
