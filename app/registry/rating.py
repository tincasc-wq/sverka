"""Рейтинг источника (ТЗ 6.6): бета-распределение с затуханием. Чистые функции, без БД.

    R = 100 · (α₀ + Σ dⱼ·cⱼ) / (α₀ + β₀ + Σ dⱼ·(cⱼ + fⱼ))

c = 1 — сообщение источника подтвердилось, f = 1 — опровергнуто;
d = 0.5^(возраст / полупериод) — старые исходы весят меньше.
"""

import math
from dataclasses import dataclass
from datetime import datetime

from app.core.scoring import SourceRatingConfig
from app.models.enums import Stance, Verdict


@dataclass(frozen=True)
class Observation:
    """Как источник выступил в истории с известным исходом."""

    story_id: int
    decided_at: datetime
    verdict: Verdict
    stance: Stance  # supports — распространял утверждение, contradicts — опровергал
    story_title: str | None = None

    @property
    def outcome(self) -> tuple[float, float] | None:
        """(c, f): оказался ли источник прав. None — исход не учитывается."""
        if self.stance == Stance.UNRELATED or self.verdict in (Verdict.MIXED, Verdict.UNKNOWN):
            return None
        source_was_right = (self.verdict == Verdict.CONFIRMED) == (
            self.stance in (Stance.SUPPORTS, Stance.REFINES)
        )
        return (1.0, 0.0) if source_was_right else (0.0, 1.0)


@dataclass(frozen=True)
class Rating:
    score: float  # 0–100
    ci_low: float
    ci_high: float
    n_observations: int
    alpha: float
    beta: float

    def shown(self, cfg: SourceRatingConfig) -> bool:
        """Мало исходов — в отчёте «недостаточно истории», число не показываем (6.6)."""
        return self.n_observations >= cfg.min_outcomes_to_show


def decay(age_days: float, half_life_days: float) -> float:
    return 0.5 ** (max(age_days, 0.0) / half_life_days)


def compute_rating(
    source_type: str,
    observations: list[Observation],
    now: datetime,
    cfg: SourceRatingConfig,
) -> Rating:
    alpha, beta = cfg.priors.get(source_type, (2.0, 2.0))
    counted = 0
    for obs in observations:
        result = obs.outcome
        if result is None:
            continue
        c, f = result
        d = decay((now - obs.decided_at).total_seconds() / 86400, cfg.half_life_days)
        alpha += d * c
        beta += d * f
        counted += 1

    mean = alpha / (alpha + beta)
    # Нормальное приближение бета-распределения: интервал широкий, пока исходов мало.
    sd = math.sqrt(alpha * beta / ((alpha + beta) ** 2 * (alpha + beta + 1)))
    low, high = max(0.0, mean - 1.96 * sd), min(1.0, mean + 1.96 * sd)
    return Rating(
        score=round(100 * mean, 1),
        ci_low=round(100 * low, 1),
        ci_high=round(100 * high, 1),
        n_observations=counted,
        alpha=alpha,
        beta=beta,
    )


def change_reason(
    previous: Rating | None,
    current: Rating,
    new_observations: list[Observation],
    source_type: str,
    cfg: SourceRatingConfig,
) -> str:
    """Человекочитаемая причина изменения — обязательна для каждой записи (приёмка этапа 5)."""
    if previous is None and not new_observations:
        a, b = cfg.priors.get(source_type, (2.0, 2.0))
        return f"Стартовая оценка по типу источника «{source_type}» (априори {a:g}/{b:g})"
    parts = []
    for obs in new_observations:
        result = obs.outcome
        if result is None:
            continue
        title = f"«{obs.story_title[:80]}»" if obs.story_title else ""
        verdict = {"confirmed": "подтвердилась", "refuted": "опровергнута"}[obs.verdict.value]
        role = "распространял" if obs.stance in (Stance.SUPPORTS, Stance.REFINES) else "опровергал"
        sign = "+" if result[0] else "−"
        parts.append(f"{sign} история #{obs.story_id} {title}: {verdict}, источник {role}")
    if not parts:
        parts.append("Затухание веса старых исходов")
    delta = current.score - (previous.score if previous else current.score)
    return f"{delta:+.1f} п. " + "; ".join(parts)
