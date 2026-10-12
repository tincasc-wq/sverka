"""Веса и пороги из config/scoring.yaml (ТЗ 6.2–6.6)."""

from functools import lru_cache
from pathlib import Path

import yaml
from pydantic import BaseModel

CONFIG_PATH = Path(__file__).resolve().parents[2] / "config" / "scoring.yaml"


class ClusteringConfig(BaseModel):
    window_hours: int = 72
    strong_threshold: float = 0.88
    include_threshold: float = 0.80
    near_hours: float = 12
    llm_review_from: float = 0.75
    candidates_top_k: int = 50
    title_trgm_threshold: float = 0.5


class GraphConfig(BaseModel):
    near_copy_max_hamming: int = 3
    near_copy_min_words: int = 20
    confidence: dict[str, float] = {
        "explicit_link": 0.95,
        "attribution": 0.8,
        "official_document": 0.6,
        "near_copy": 0.7,
    }
    unreliable_date_hours: float = 2
    # Заявленная дата может опережать наш first_seen_at не больше чем на интервал опроса.
    clock_tolerance_minutes: float = 10
    attribution_window_chars: int = 60


class SourceRatingConfig(BaseModel):
    half_life_days: float = 180
    silent_edit_penalty: float = 0.5
    min_outcomes_to_show: int = 10
    # alpha0 / beta0 по типу источника: стартовое доверие и его «вес» в наблюдениях.
    priors: dict[str, tuple[float, float]] = {
        "agency": (6, 2),
        "official": (6, 2),
        "media": (3, 2),
        "tg": (2, 2),
        "vk": (2, 2),
    }
    # Меньше этого изменения рейтинга новую запись в историю не пишем (шум затухания).
    min_score_change: float = 0.5


class ScoringConfig(BaseModel):
    clustering: ClusteringConfig = ClusteringConfig()
    graph: GraphConfig = GraphConfig()
    source_rating: SourceRatingConfig = SourceRatingConfig()


@lru_cache
def get_scoring(path: Path = CONFIG_PATH) -> ScoringConfig:
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return ScoringConfig.model_validate(
        {k: v for k, v in data.items() if k in ScoringConfig.model_fields}
    )
