"""Опрос всех отслеживаемых источников. Падение одного источника не останавливает остальные."""

import asyncio
import logging
from collections import Counter
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta

import httpx
from sqlalchemy import ColumnElement, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.collectors import robots, rss, telegram
from app.collectors.http import fetch, fetch_bytes, make_client
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
    raw, final_url = await fetch(client, telegram.preview_url(source.handle_or_domain))
    if not final_url.path.startswith("/s/"):
        # Канал отключил веб-превью: t.me уводит на страницу без постов.
        raise telegram.PreviewDisabled(f"web preview disabled, redirected to {final_url}")
    docs = telegram.parse_channel_page(raw, source.id)
    unsupported = telegram.unsupported_posts(raw)
    if unsupported:
        # Новые посты, которых ещё нет в архиве, дочитываем по мета-описанию страницы поста.
        from app.collectors.store import known_urls
        from app.core.db import SessionLocal

        async with SessionLocal() as session:
            known = await known_urls(session, [url for url, _ in unsupported])
        fresh = [(url, when) for url, when in unsupported if url not in known]
        docs += await telegram.fetch_unsupported(client, fresh, source.id)
    return docs


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


# Записи старше трёх дней из ленты не берём: у нового источника лента бывает архивом за годы
# (CDC — 1844 записи), а заброшенный канал при первом опросе выглядел живым из-за постов
# 2024 года («Кремлёвский пул РИА», 12.10.2026). Время первого появления у таких записей
# было бы ложным — «Сверка» увидела их не тогда, когда они вышли.
MAX_ENTRY_AGE = timedelta(days=3)


def _fresh(docs: list[DocumentIn]) -> list[DocumentIn]:
    since = datetime.now(UTC) - MAX_ENTRY_AGE
    return [d for d in docs if d.published_at is None or d.published_at >= since]


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
            docs = fetched = _fresh(_unique_by_url(await fetch(client, source)))
            if full_text:
                # Полный текст качаем только для новых ссылок: лента повторяет старые записи.
                known = await known_urls(session, [d.url for d in docs])
                new_docs = [d for d in docs if normalize_url(d.url) not in known]
                await rss.enrich_full_text(client, new_docs)
                docs = new_docs
            for doc in docs:
                stats[await save_document(session, doc)] += 1
            src = await session.get(Source, source.id)
            if source.feed_url:
                sample = fetched[0].url if fetched else None
                note = await robots.note(client, source.feed_url, sample)
                if note != "":
                    src.robots_note = note
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


# Сломанный источник не дёргаем каждые 5 минут: после трёх ошибок подряд пауза растёт вдвое
# (10, 20, 40… до 160 минут). The Washington Post с 14.10.2026 отвечал 502 — 1543 запроса впустую,
# Phys.org отвечал 429 «слишком много запросов».
BACKOFF = text(
    "(sources.consecutive_errors < 3 or sources.last_polled_at is null"
    " or sources.last_polled_at < now()"
    " - least(160, 5 * power(2, least(sources.consecutive_errors, 7) - 2)) * interval '1 minute')"
)

# Редкий опрос по просьбе сайта: Phys.org отвечал 429 «слишком много запросов» (20.10.2026).
DUE = text(
    "(sources.poll_minutes is null or sources.last_polled_at is null"
    " or sources.last_polled_at < now() - sources.poll_minutes * interval '1 minute')"
)


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
        query = select(Source).where(Source.is_tracked, condition, BACKOFF, DUE).order_by(Source.id)
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
    settings = get_settings()
    direct = poll_sources(
        sessionmaker,
        Source.feed_url.is_not(None) & Source.via_gateway.is_(False),
        fetch_rss_source,
        full_text=settings.fetch_full_text,
        client=client,
    )
    # Источники, недоступные из РФ (или не пускающие российские адреса), — через шлюз.
    proxy = settings.collector_proxy or settings.telegram_proxy
    if client is None and not proxy:
        return await direct
    gateway = client or make_client(proxy=proxy)
    try:
        # Параллельно, а не после прямых: долгий прямой цикл (14.10.2026 — после
        # восстановления базы) иначе съедал весь таймаут, и до шлюза очередь не доходила.
        results = await asyncio.gather(
            direct,
            poll_sources(
                sessionmaker,
                Source.feed_url.is_not(None) & Source.via_gateway.is_(True),
                fetch_rss_source,
                full_text=settings.fetch_full_text,
                client=gateway,
            ),
        )
    finally:
        if client is None:
            await gateway.aclose()
    total = Counter()
    for counter in results:
        total.update(counter)
    return total


async def poll_telegram(sessionmaker, client: httpx.AsyncClient | None = None) -> Counter:
    # Тип источника (официальный, СМИ) и канал доставки независимы: TG-канал узнаём по «@».
    settings = get_settings()
    client = client or make_client(proxy=settings.telegram_proxy, ipv6=settings.telegram_ipv6)
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
