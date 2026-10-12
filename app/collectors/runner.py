"""Опрос всех отслеживаемых источников. Падение одного источника не останавливает остальные."""

import asyncio
import logging
from collections import Counter
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime

import httpx
from sqlalchemy import ColumnElement, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.collectors import rss, telegram
from app.collectors.http import fetch_bytes, make_client
from app.collectors.store import DocumentIn, SaveResult, known_urls, save_document
from app.collectors.urls import normalize_url
from app.core.config import get_settings
from app.models import Source

log = logging.getLogger(__name__)

Fetcher = Callable[[httpx.AsyncClient, Source], Awaitable[list[DocumentIn]]]


async def fetch_rss_source(client: httpx.AsyncClient, source: Source) -> list[DocumentIn]:
    raw = await fetch_bytes(client, source.feed_url)
    return rss.parse_feed(raw, source.id)


async def fetch_telegram_source(client: httpx.AsyncClient, source: Source) -> list[DocumentIn]:
    raw = await fetch_bytes(client, telegram.preview_url(source.handle_or_domain))
    return telegram.parse_channel_page(raw, source.id)


def _unique_by_url(docs: list[DocumentIn]) -> list[DocumentIn]:
    """Ленты бывает дублируют запись (одна статья в нескольких рубриках) — берём первую."""
    seen: set[str] = set()
    unique = []
    for doc in docs:
        url = normalize_url(doc.url)
        if url not in seen:
            seen.add(url)
            unique.append(doc)
    return unique


async def _poll_one(
    sessionmaker: async_sessionmaker[AsyncSession],
    client: httpx.AsyncClient,
    source: Source,
    fetch: Fetcher,
    full_text: bool,
) -> Counter:
    stats: Counter = Counter()
    async with sessionmaker() as session:
        try:
            docs = _unique_by_url(await fetch(client, source))
            if full_text:
                # Полный текст качаем только для новых ссылок: лента повторяет старые записи.
                known = await known_urls(session, [d.url for d in docs])
                new_docs = [d for d in docs if normalize_url(d.url) not in known]
                await rss.enrich_full_text(client, new_docs)
                docs = new_docs
            for doc in docs:
                stats[await save_document(session, doc)] += 1
            src = await session.get(Source, source.id)
            src.last_polled_at = datetime.now(UTC)
            src.last_error = None
            src.consecutive_errors = 0
            await session.commit()
        except Exception as exc:  # noqa: BLE001 — любая ошибка источника изолируется
            await session.rollback()
            log.warning("source %s failed: %r", source.handle_or_domain, exc)
            src = await session.get(Source, source.id)
            src.last_polled_at = datetime.now(UTC)
            src.last_error = f"{type(exc).__name__}: {exc}"[:500]
            src.consecutive_errors += 1
            await session.commit()
            stats["failed_sources"] += 1
    return stats


async def poll_sources(
    sessionmaker: async_sessionmaker[AsyncSession],
    condition: ColumnElement[bool],
    fetch: Fetcher,
    *,
    full_text: bool,
    client: httpx.AsyncClient | None = None,
) -> Counter:
    settings = get_settings()
    async with sessionmaker() as session:
        query = select(Source).where(Source.is_tracked, condition).order_by(Source.id)
        sources = list(await session.scalars(query))

    owns_client = client is None
    client = client or make_client()
    semaphore = asyncio.Semaphore(settings.collector_concurrency)

    async def guarded(source: Source) -> Counter:
        async with semaphore:
            return await _poll_one(sessionmaker, client, source, fetch, full_text)

    try:
        results = await asyncio.gather(*(guarded(s) for s in sources))
    finally:
        if owns_client:
            await client.aclose()

    total: Counter = Counter({"sources": len(sources)})
    for r in results:
        total.update(r)
    return total


async def poll_rss(sessionmaker, client: httpx.AsyncClient | None = None) -> Counter:
    return await poll_sources(
        sessionmaker,
        Source.feed_url.is_not(None),
        fetch_rss_source,
        full_text=get_settings().fetch_full_text,
        client=client,
    )


async def poll_telegram(sessionmaker, client: httpx.AsyncClient | None = None) -> Counter:
    # Тип источника (официальный, СМИ) и канал доставки независимы: TG-канал узнаём по «@».
    client = client or make_client(proxy=get_settings().telegram_proxy)
    try:
        return await poll_sources(
            sessionmaker,
            Source.handle_or_domain.startswith("@"),
            fetch_telegram_source,
            full_text=False,
            client=client,
        )
    finally:
        await client.aclose()


def summarize(stats: Counter) -> str:
    keys = ["sources", "failed_sources", *(r.value for r in SaveResult)]
    return ", ".join(f"{k}={stats.get(k, 0)}" for k in keys)
