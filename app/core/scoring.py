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
    crosslingual_offset: float = 0.08  # прибавка к близости пар на разных языках


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
        "satire": (1, 9),
        "aggregator": (2, 2),
    }
    # Меньше этого изменения рейтинга новую запись в историю не пишем (шум затухания).
    min_score_change: float = 0.5


class IndexConfig(BaseModel):
    """Индекс подтверждённости (6.5); значения — в секции confirmation_index scoring.yaml."""

    type_weights: dict[str, float] = {
        "official": 2.0,
        "agency": 1.5,
        "media": 1.0,
        "tg": 0.5,
        "vk": 0.5,
        "satire": 0.0,
        "aggregator": 0.0,
    }
    stance_sign: dict[str, float] = {
        "supports": 1.0,
        "contradicts": -1.0,
        "corrects": -1.5,
        "refines": 0.5,
        "unrelated": 0.0,
    }
    # Поправка от независимого источника: утверждение «оспорено», индекс не выше этого —
    # сколько бы изданий ни повторили ошибку раньше (случай Minerva Zen, 14.10.2026).
    correction_cap: float = 35
    main_claim_weight: float = 2.0
    too_early_hours: float = 6
    statuses: dict[str, float] = {
        "widely_confirmed": 75,
        "partially_confirmed": 50,
        "not_confirmed": 25,
    }
    max_verify_documents: int = 12  # со сколькими публикациями истории сверять утверждения
    # Полнота архива на момент новости: доля источников, от которых есть публикации в окне
    # ±coverage_window_hours, среди источников, активных за последние сутки.
    coverage_window_hours: float = 6
    coverage_min_share: float = 0.5


class ScoringConfig(BaseModel):
    clustering: ClusteringConfig = ClusteringConfig()
    graph: GraphConfig = GraphConfig()
    source_rating: SourceRatingConfig = SourceRatingConfig()
    confirmation_index: IndexConfig = IndexConfig()


@lru_cache
def get_scoring(path: Path = CONFIG_PATH) -> ScoringConfig:
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return ScoringConfig.model_validate(
        {k: v for k, v in data.items() if k in ScoringConfig.model_fields}
    )
