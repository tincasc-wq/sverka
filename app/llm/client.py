"""Адаптер LLM (ТЗ, раздел 8.1): один OpenAI-совместимый клиент для всех провайдеров.

- маршрут шага — список моделей из config/llm.yaml, порядок = порядок перехода на резерв;
- ответ — JSON, проверенный по Pydantic-схеме; невалидный — одна повторная попытка;
- каждый вызов (и каждый переход на резерв с причиной) пишется в llm_calls;
- дневной потолок расходов по провайдеру — провайдер пропускается, если исчерпан.
"""

import asyncio
import copy
import json
import logging
import time
from datetime import UTC, datetime
from decimal import Decimal
from typing import TypeVar

import httpx
from pydantic import BaseModel, ValidationError
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.llm.config import LlmConfig, Route, get_llm_config
from app.models import LlmCall

log = logging.getLogger(__name__)
T = TypeVar("T", bound=BaseModel)

_API_KEYS = {
    "deepseek": "deepseek_api_key",
    "qwen": "qwen_api_key",
    "yandex": "yandex_ai_studio_api_key",
    "gigachat": "gigachat_auth_key",
    "anthropic": "anthropic_api_key",
}


def strip_fences(content: str) -> str:
    """Модели без JSON-режима заворачивают ответ в ```json … ``` — снимаем обёртку."""
    text = content.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1] if "\n" in text else text[3:]
        text = text.rsplit("```", 1)[0]
    return text.strip()


class LlmUnavailable(RuntimeError):
    """Ни одна модель маршрута не ответила валидно."""


class _BadAnswer(Exception):
    pass


class LlmClient:
    def __init__(
        self,
        config: LlmConfig | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        api_keys: dict[str, str] | None = None,
    ) -> None:
        self.config = config or get_llm_config()
        self._transport = transport
        settings = get_settings()
        self._keys = api_keys or {
            name: getattr(settings, field) for name, field in _API_KEYS.items()
        }
        self._proxy = settings.llm_proxy or None
        # Сетевые запросы шагов идут параллельно, а сессия БД одна — операции с ней по очереди.
        self._db_lock = asyncio.Lock()
        self.ru_only = False
        self._settings = settings
        self._tokens: dict[str, tuple[str, float]] = {}  # провайдер → (токен, до какого времени)

    def restricted(self, ru_only: bool) -> "LlmClient":
        """Клиент для госконтура: любой шаг — только по ru_only_routes (раздел 12.5).

        Ограничение держит сам клиент, а не каждый вызов: шагов много, и забытый флаг
        в одном из них отправил бы запрос аппарата в зарубежную модель.
        """
        if not ru_only:
            return self
        clone = copy.copy(self)
        clone._db_lock = asyncio.Lock()
        clone.ru_only = True
        return clone

    def available(self) -> bool:
        if self.ru_only:
            return any(self._routes(step, True) for step in self.config.ru_only_routes)
        return any(p.enabled and self._keys.get(name) for name, p in self.config.providers.items())

    def _routes(self, step: str, ru_only: bool) -> list[Route]:
        routes = (self.config.ru_only_routes if ru_only else self.config.routes).get(step, [])
        return [
            r
            for r in routes
            if (p := self.config.providers.get(r.provider))
            and p.enabled
            and self._keys.get(r.provider)
        ]

    @staticmethod
    async def _spent_today(session: AsyncSession, provider: str) -> float:
        start = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
        total = await session.scalar(
            select(func.coalesce(func.sum(LlmCall.cost_usd), 0)).where(
                LlmCall.provider == provider, LlmCall.created_at >= start
            )
        )
        return float(total)

    def _verify(self, provider_name: str):
        bundle = self._settings.gigachat_ca_bundle if provider_name == "gigachat" else ""
        return bundle or True

    async def _token(self, name: str) -> str:
        """Токен доступа GigaChat: ключ авторизации → токен на 30 минут, берём с запасом."""
        cached = self._tokens.get(name)
        if cached and cached[1] > time.time() + 60:
            return cached[0]
        import uuid

        provider = self.config.providers[name]
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(30, connect=10),
            verify=self._verify(name),
            transport=self._transport,
        ) as client:
            response = await client.post(
                provider.auth_url,
                headers={
                    "Authorization": f"Basic {self._keys[name]}",
                    "RqUID": str(uuid.uuid4()),
                    "Accept": "application/json",
                },
                data={"scope": self._settings.gigachat_scope},
            )
            response.raise_for_status()
            data = response.json()
        expires = float(data.get("expires_at") or 0) / 1000 or time.time() + 1500
        self._tokens[name] = (data["access_token"], expires)
        return data["access_token"]

    async def _client(self, route: Route) -> httpx.AsyncClient:
        provider = self.config.providers[route.provider]
        if provider.auth == "gigachat":
            token = await self._token(route.provider)
        else:
            token = self._keys[route.provider]
        headers = {"Authorization": f"Bearer {token}"}
        if route.provider == "yandex" and self._settings.yandex_folder_id:
            headers["x-folder-id"] = self._settings.yandex_folder_id
        return httpx.AsyncClient(
            base_url=provider.base_url,
            timeout=httpx.Timeout(120, connect=10),
            headers=headers,
            proxy=self._proxy if provider.via_gateway else None,
            verify=self._verify(route.provider),
            transport=self._transport,
        )

    def _model(self, route: Route) -> str:
        # Yandex AI Studio ждёт полный адрес модели: gpt://<каталог>/<модель>[/latest].
        folder = self._settings.yandex_folder_id
        if route.provider == "yandex" and folder and not route.model.startswith("gpt://"):
            return f"gpt://{folder}/{route.model}"
        return route.model

    async def _call(self, route: Route, system: str, user: str) -> tuple[str, dict]:
        payload: dict = {
            "model": self._model(route),
            "temperature": 0,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        }
        if self.config.providers[route.provider].json_mode:
            payload["response_format"] = {"type": "json_object"}
        if route.thinking:
            payload["thinking"] = {"type": route.thinking}
        if route.reasoning_effort:
            payload["reasoning_effort"] = route.reasoning_effort
        async with await self._client(route) as client:
            response = await client.post("/chat/completions", json=payload)
            response.raise_for_status()
            data = response.json()
        return data["choices"][0]["message"]["content"], data.get("usage") or {}

    async def complete_json(
        self,
        session: AsyncSession,
        step: str,
        system: str,
        user: str,
        schema: type[T],
        *,
        check_id: int | None = None,
        prompt_version: str | None = None,
        ru_only: bool = False,
    ) -> T:
        routes = self._routes(step, ru_only or self.ru_only)
        if not routes:
            raise LlmUnavailable(f"no enabled route with API key for step {step}")
        fallback_reason: str | None = None
        for route in routes:
            budget = self.config.daily_budget_usd.get(route.provider)
            if budget is not None:
                async with self._db_lock:
                    spent = await self._spent_today(session, route.provider)
            if budget is not None and spent >= budget:
                fallback_reason = f"{route.provider}: daily budget {budget} USD exhausted"
                continue
            for _attempt in (1, 2):
                started = time.monotonic()
                usage: dict = {}
                try:
                    content, usage = await self._call(route, system, user)
                    result = schema.model_validate(json.loads(strip_fences(content)))
                except (httpx.HTTPError, KeyError) as exc:
                    error = f"{route.provider}/{route.model}: {type(exc).__name__}: {exc}"[:500]
                    await self._record(
                        session, route, step, check_id, prompt_version, usage, started, error
                    )
                    fallback_reason = error
                    break  # сеть или API — сразу следующая модель
                except (json.JSONDecodeError, ValidationError) as exc:
                    error = f"{route.provider}/{route.model}: invalid JSON ({type(exc).__name__})"
                    await self._record(
                        session, route, step, check_id, prompt_version, usage, started, error
                    )
                    fallback_reason = error
                    continue  # одна повторная попытка на той же модели
                await self._record(
                    session, route, step, check_id, prompt_version, usage, started, fallback_reason
                )
                return result
        raise LlmUnavailable(fallback_reason or f"all routes failed for {step}")

    async def _record(
        self, session, route, step, check_id, prompt_version, usage, started, reason
    ) -> None:
        """Пишет вызов в сессию вызывающего кода: учёт сохранится вместе с самой проверкой."""
        tokens_in = int(usage.get("prompt_tokens") or 0)
        tokens_out = int(usage.get("completion_tokens") or 0)
        cached_in = int(usage.get("prompt_cache_hit_tokens") or 0)  # поле DeepSeek
        cost = self.config.cost_usd(route.model, tokens_in, tokens_out, cached_in)
        async with self._db_lock:
            session.add(
                LlmCall(
                    check_id=check_id,
                    stage=step,
                    provider=route.provider,
                    model=route.model,
                    prompt_version=prompt_version,
                    tokens_in=tokens_in,
                    tokens_out=tokens_out,
                    cost_usd=Decimal(str(round(cost, 6))),
                    latency_ms=int((time.monotonic() - started) * 1000),
                    fallback_reason=reason,
                )
            )
            await session.flush()
