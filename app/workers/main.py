"""Фоновый воркер arq. На этапе 1 — только проверочная задача; конвейер появится на этапе 3."""

from arq.connections import RedisSettings

from app.core.config import get_settings


async def ping(ctx: dict) -> str:
    return "pong"


class WorkerSettings:
    functions = [ping]
    redis_settings = RedisSettings.from_dsn(get_settings().redis_url)
