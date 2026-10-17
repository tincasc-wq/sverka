"""Провайдеры, маршруты и цены LLM из config/llm.yaml (ТЗ, раздел 8.1)."""

from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path

import yaml
from pydantic import BaseModel

CONFIG_PATH = Path(__file__).resolve().parents[2] / "config" / "llm.yaml"


class Provider(BaseModel):
    enabled: bool = True
    base_url: str = ""
    via_gateway: bool = False
    auth: str = "bearer"  # bearer | gigachat (ключ меняется на токен доступа)
    auth_url: str = ""  # для gigachat — адрес выдачи токена
    json_mode: bool = True  # поддерживает response_format: json_object


class Route(BaseModel):
    provider: str
    model: str
    thinking: str | None = None  # enabled | disabled (DeepSeek)
    reasoning_effort: str | None = None


class Price(BaseModel):
    input: float  # USD за 1 млн входных токенов без кэша (пиковый тариф)
    output: float
    cached_input: float | None = None  # вход, совпавший с кэшем провайдера
    offpeak_factor: float = 1.0  # множитель вне пиковых часов (DeepSeek — 0.5)


class LlmConfig(BaseModel):
    providers: dict[str, Provider]
    routes: dict[str, list[Route]]
    ru_only_routes: dict[str, list[Route]] = {}
    daily_budget_usd: dict[str, float] = {}
    prices: dict[str, Price] = {}
    # Пиковые часы DeepSeek по UTC, только будни: [[начало, конец), ...]
    peak_hours_utc: list[tuple[int, int]] = []

    def is_peak(self, at: datetime) -> bool:
        at = at.astimezone(UTC)
        return at.weekday() < 5 and any(a <= at.hour < b for a, b in self.peak_hours_utc)

    def cost_usd(
        self,
        model: str,
        tokens_in: int,
        tokens_out: int,
        cached_in: int = 0,
        at: datetime | None = None,
    ) -> float:
        """Стоимость вызова так, как её считает провайдер: кэш входа и скидка вне пика."""
        price = self.prices.get(model)
        if price is None:
            return 0.0
        cached = min(cached_in, tokens_in) if price.cached_input is not None else 0
        usd = (
            (tokens_in - cached) * price.input
            + cached * (price.cached_input or 0)
            + tokens_out * price.output
        ) / 1_000_000
        if not self.is_peak(at or datetime.now(UTC)):
            usd *= price.offpeak_factor
        return usd


@lru_cache
def get_llm_config(path: Path = CONFIG_PATH) -> LlmConfig:
    return LlmConfig.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))
