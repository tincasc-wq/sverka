"""Очередь arq для сайта: проверки выполняет тот же воркер, что и для бота."""

from arq import ArqRedis, create_pool
from arq.connections import RedisSettings

from app.core.config import get_settings

_pool: ArqRedis | None = None


async def enqueue(job: str, *args, **options) -> None:
    global _pool
    if _pool is None:
        _pool = await create_pool(RedisSettings.from_dsn(get_settings().redis_url))
    await _pool.enqueue_job(job, *args, **options)
