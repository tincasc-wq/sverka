"""Индекс подтверждённости 0–100 (ТЗ 6.5). Чистые функции, без БД и без LLM.

    I = 100 · σ( b₀ + Σ_{независимые группы} s · w_тип · R/100 )

b₀ = (R₀ − 50)/25 — смещение по рейтингу первоисточника; s — знак позиции группы
(+1 подтверждает, −1 противоречит, +0.5 уточняет); w — вес типа источника; R — рейтинг.
Независимая группа — группа владения, чья первая публикация в истории не заимствована
(корень графа). Перепечатки подтверждением не считаются.
"""

import math
from dataclasses import dataclass, field

from app.core.scoring import IndexConfig

STANCE_PRIORITY = {"contradicts": 3, "supports": 2, "refines": 1, "unrelated": 0}


@dataclass(frozen=True)
class Evidence:
    group: str
    source_type: str
    rating: float | None  # рейтинг источника 0–100; None — нет в реестре
    stance: str
    independent: bool


@dataclass
class ClaimScore:
    index: float
    status: str
    independent_groups: dict[str, str] = field(default_factory=dict)  # группа → позиция


def sigmoid(x: float) -> float:
    return 1 / (1 + math.exp(-x))


def status_for(index: float, cfg: IndexConfig) -> str:
    s = cfg.statuses
    if index >= s["widely_confirmed"]:
        return "widely_confirmed"
    if index >= s["partially_confirmed"]:
        return "partially_confirmed"
    if index >= s["not_confirmed"]:
        return "not_confirmed"
    return "refuted"


def score_claim(
    evidence: list[Evidence], primary_rating: float | None, cfg: IndexConfig
) -> ClaimScore:
    # Одна группа — один голос: сильнейшая позиция (противоречие важнее подтверждения).
    by_group: dict[str, Evidence] = {}
    for e in evidence:
        if not e.independent or e.stance == "unrelated":
            continue
        current = by_group.get(e.group)
        if current is None or STANCE_PRIORITY[e.stance] > STANCE_PRIORITY[current.stance]:
            by_group[e.group] = e
    b0 = ((primary_rating if primary_rating is not None else 50.0) - 50) / 25
    total = b0 + sum(
        cfg.stance_sign[e.stance]
        * cfg.type_weights.get(e.source_type, 1.0)
        * (e.rating if e.rating is not None else 50.0)
        / 100
        for e in by_group.values()
    )
    index = round(100 * sigmoid(total), 1)
    # Без независимых сообщений число отражает только репутацию первоисточника —
    # называть это «подтверждено» нельзя.
    status = status_for(index, cfg) if by_group else "not_confirmed"
    return ClaimScore(index, status, {g: e.stance for g, e in by_group.items()})


@dataclass
class NewsScore:
    index: float | None
    status: str
    claims: list[ClaimScore]


def score_news(
    claims: list[list[Evidence]],
    checkable: list[bool],
    primary_rating: float | None,
    hours_since_publication: float,
    cfg: IndexConfig,
) -> NewsScore:
    scores = [score_claim(ev, primary_rating, cfg) for ev in claims]
    weighted = [
        (s.index, cfg.main_claim_weight if i == 0 else 1.0)
        for i, (s, ok) in enumerate(zip(scores, checkable, strict=True))
        if ok
    ]
    any_independent = any(
        s.independent_groups for s, ok in zip(scores, checkable, strict=True) if ok
    )
    if not weighted:
        return NewsScore(None, "no_checkable_claims", scores)
    if not any_independent and hours_since_publication < cfg.too_early_hours:
        return NewsScore(None, "too_early", scores)
    index = round(sum(i * w for i, w in weighted) / sum(w for _, w in weighted), 1)
    status = status_for(index, cfg) if any_independent else "not_confirmed"
    return NewsScore(index, status, scores)
