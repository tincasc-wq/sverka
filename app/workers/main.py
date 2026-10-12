"""Фоновый воркер arq: сборщики по расписанию и расчёт эмбеддингов (этап 2)."""

import logging

from arq import cron
from arq.connections import RedisSettings

from app.collectors.runner import poll_rss, poll_telegram, summarize
from app.core.config import get_settings
from app.core.db import SessionLocal
from app.embeddings import SentenceTransformerEmbedder, embed_pending

log = logging.getLogger("sverka.worker")


async def startup(ctx: dict) -> None:
    ctx["embedder"] = SentenceTransformerEmbedder()


async def ping(ctx: dict) -> str:
    return "pong"


async def collect_rss(ctx: dict) -> str:
    result = summarize(await poll_rss(SessionLocal))
    log.info("rss: %s", result)
    return result


async def collect_telegram(ctx: dict) -> str:
    result = summarize(await poll_telegram(SessionLocal))
    log.info("telegram: %s", result)
    return result


async def embed_documents(ctx: dict) -> int:
    async with SessionLocal() as session:
        count = await embed_pending(session, ctx["embedder"])
    if count:
        log.info("embedded %d documents", count)
    return count


class WorkerSettings:
    functions = [ping, collect_rss, collect_telegram, embed_documents]
    cron_jobs = [
        cron(collect_rss, minute=set(range(0, 60, 5)), unique=True, timeout=600),
        cron(collect_telegram, minute=set(range(2, 60, 10)), unique=True, timeout=600),
        cron(embed_documents, second=30, unique=True, timeout=900),
    ]
    on_startup = startup
    redis_settings = RedisSettings.from_dsn(get_settings().redis_url)
    max_jobs = 4
