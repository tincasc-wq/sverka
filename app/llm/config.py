"""Провайдеры, маршруты и цены LLM из config/llm.yaml (ТЗ, раздел 8.1)."""

from functools import lru_cache
from pathlib import Path

import yaml
from pydantic import BaseModel

CONFIG_PATH = Path(__file__).resolve().parents[2] / "config" / "llm.yaml"


class Provider(BaseModel):
    enabled: bool = True
    base_url: str = ""
    via_gateway: bool = False


class Route(BaseModel):
    provider: str
    model: str
    thinking: str | None = None  # enabled | disabled (DeepSeek)
    reasoning_effort: str | None = None


class Price(BaseModel):
    input: float
    output: float


class LlmConfig(BaseModel):
    providers: dict[str, Provider]
    routes: dict[str, list[Route]]
    ru_only_routes: dict[str, list[Route]] = {}
    daily_budget_usd: dict[str, float] = {}
    prices: dict[str, Price] = {}

    def cost_usd(self, model: str, tokens_in: int, tokens_out: int) -> float:
        price = self.prices.get(model)
        if price is None:
            return 0.0
        return (tokens_in * price.input + tokens_out * price.output) / 1_000_000


@lru_cache
def get_llm_config(path: Path = CONFIG_PATH) -> LlmConfig:
    return LlmConfig.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))
