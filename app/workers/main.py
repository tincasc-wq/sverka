"""Фоновый воркер arq: сборщики по расписанию и расчёт эмбеддингов (этап 2)."""

import asyncio
import functools
import logging
from collections.abc import Awaitable, Callable

from arq import cron
from arq.connections import RedisSettings

from app.collectors.runner import poll_rss, poll_telegram, summarize
from app.core.config import get_settings
from app.core.db import SessionLocal
from app.embeddings import SentenceTransformerEmbedder, embed_pending
from app.registry.service import recompute_ratings

log = logging.getLogger("sverka.worker")


def single_flight(func: Callable[[dict], Awaitable]) -> Callable[[dict], Awaitable]:
    """Пропускает запуск, если предыдущий ещё идёт: крон не должен копить параллельные копии."""
    lock = asyncio.Lock()

    @functools.wraps(func)
    async def wrapper(ctx: dict):
        if lock.locked():
            return "skipped: previous run in progress"
        async with lock:
            return await func(ctx)

    return wrapper


async def startup(ctx: dict) -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(asctime)s %(name)s: %(message)s", "%H:%M:%S"))
    sverka_log = logging.getLogger("sverka")
    sverka_log.addHandler(handler)
    sverka_log.setLevel(logging.INFO)
    ctx["embedder"] = SentenceTransformerEmbedder()


async def ping(ctx: dict) -> str:
    return "pong"


@single_flight
async def collect_rss(ctx: dict) -> str:
    result = summarize(await poll_rss(SessionLocal))
    log.info("rss: %s", result)
    return result


@single_flight
async def collect_telegram(ctx: dict) -> str:
    result = summarize(await poll_telegram(SessionLocal))
    log.info("telegram: %s", result)
    return result


@single_flight
async def embed_documents(ctx: dict) -> int:
    async with SessionLocal() as session:
        count = await embed_pending(session, ctx["embedder"])
    if count:
        log.info("embedded %d documents", count)
    return count


@single_flight
async def recompute_source_ratings(ctx: dict) -> str:
    async with SessionLocal() as session:
        stats = await recompute_ratings(session)
        await session.commit()
    result = f"sources={stats.sources}, changed={stats.changed}"
    log.info("ratings: %s", result)
    return result


class WorkerSettings:
    functions = [ping, collect_rss, collect_telegram, embed_documents, recompute_source_ratings]
    cron_jobs = [
        cron(collect_rss, minute=set(range(0, 60, 5)), unique=True, timeout=600),
        cron(collect_telegram, minute=set(range(2, 60, 10)), unique=True, timeout=600),
        cron(embed_documents, second=30, unique=True, timeout=900),
        # Ночной пересчёт рейтинга (6.6); 03:00 по Москве = 00:00 UTC.
        cron(recompute_source_ratings, hour=0, minute=0, unique=True, timeout=1800),
    ]
    on_startup = startup
    redis_settings = RedisSettings.from_dsn(get_settings().redis_url)
    max_jobs = 8
