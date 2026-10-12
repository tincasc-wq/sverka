"""Проверка публикации из консоли (этап 3, детерминированная часть).

  python -m app.check <url>       # публикация по ссылке (скачается, если её нет в архиве)
  python -m app.check doc:<id>    # публикация из архива по id

Если у публикации нет эмбеддинга, команду нужно запускать в контейнере worker (там модель).
"""

import asyncio
import json
import secrets
import sys
from datetime import UTC, datetime, timedelta

import trafilatura
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.collectors import rss, telegram
from app.collectors.http import fetch, make_client
from app.collectors.store import DocumentIn, save_document
from app.collectors.urls import domain_of, normalize_url
from app.core.config import get_settings
from app.core.db import SessionLocal
from app.core.scoring import get_scoring
from app.embeddings import Embedder, document_text
from app.models import Check, Document, Source
from app.models.enums import CheckStatus, InputType
from app.pipeline.report import build_report
from app.pipeline.story import build_story


async def _source_for(session: AsyncSession, url: str) -> int | None:
    if domain_of(url) == "t.me":
        handle = "@" + url.split("/")[3]
    else:
        handle = domain_of(url)
    return await session.scalar(select(Source.id).where(Source.handle_or_domain == handle))


async def _download(url: str, source_id: int | None) -> DocumentIn:
    """Скачивает публикацию, которой ещё нет в архиве."""
    if domain_of(url) == "t.me":
        proxy = get_settings().telegram_proxy
        async with make_client(proxy=proxy, public_only=True) as client:
            raw, _ = await fetch(client, f"{url}?embed=1")
        docs = telegram.parse_channel_page(raw, source_id)
        if not docs:
            raise ValueError(f"no text found in Telegram post {url}")
        docs[0].url = url
        return docs[0]

    async with make_client(public_only=True) as client:
        raw, _ = await fetch(client, url)
    meta = trafilatura.extract_metadata(raw)
    published = None
    if meta and meta.date:
        published = datetime.fromisoformat(meta.date).replace(tzinfo=UTC)
    return DocumentIn(
        url=url,
        source_id=source_id,
        title=meta.title if meta else None,
        text=rss.extract_article_text(raw, url),
        published_at=published,
        links=rss.extract_article_links(raw, url),
    )


async def resolve_seed(session: AsyncSession, arg: str) -> Document:
    if arg.startswith("doc:"):
        doc = await session.get(Document, int(arg.removeprefix("doc:")))
        if doc is None:
            raise ValueError(f"no document {arg}")
        return doc
    url = normalize_url(arg)
    doc = await session.scalar(select(Document).where(Document.url == url))
    if doc is None:
        await save_document(session, await _download(arg, await _source_for(session, url)))
        doc = await session.scalar(select(Document).where(Document.url == url))
    return doc


async def recent_check(session: AsyncSession, document_id: int, hours: float) -> Check | None:
    """Готовый отчёт по той же публикации моложе hours часов (ФТ-1.3)."""
    since = datetime.now(UTC) - timedelta(hours=hours)
    return await session.scalar(
        select(Check)
        .where(
            Check.document_id == document_id,
            Check.status == CheckStatus.DONE,
            Check.finished_at >= since,
        )
        .order_by(Check.finished_at.desc())
        .limit(1)
    )


async def run_check(
    session: AsyncSession,
    arg: str,
    embedder: Embedder | None,
    *,
    user_id: int | None = None,
    input_type: InputType = InputType.URL,
    cache_hours: float = 0,
) -> Check:
    seed = await resolve_seed(session, arg)
    if cache_hours and (cached := await recent_check(session, seed.id, cache_hours)):
        await session.commit()  # сохранить скачанную публикацию, если её не было
        return cached
    if seed.embedding is None:
        if embedder is None:
            raise RuntimeError("seed has no embedding: run in the worker container")
        settings = get_settings()
        [vector] = await asyncio.to_thread(
            embedder.embed, [document_text(seed, settings.embedding_max_chars)]
        )
        seed.embedding = vector
        await session.flush()

    result = await build_story(session, seed)
    report = build_report(result, get_scoring().graph)
    check = Check(
        user_id=user_id,
        input_type=input_type,
        input_raw=arg,
        document_id=seed.id,
        story_id=result.story_id,
        status=CheckStatus.DONE,
        summary=json.dumps(report, ensure_ascii=False),
        finished_at=datetime.now(UTC),
        public_token=secrets.token_urlsafe(12),
    )
    session.add(check)
    await session.commit()
    return check


def _embedder() -> Embedder | None:
    try:
        import sentence_transformers  # noqa: F401 — есть только в образе воркера

        from app.embeddings import SentenceTransformerEmbedder

        return SentenceTransformerEmbedder()
    except ImportError:
        return None


async def _main(arg: str) -> None:
    async with SessionLocal() as session:
        check = await run_check(session, arg, _embedder())
    print(json.dumps(json.loads(check.summary), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print(__doc__)
        sys.exit(1)
    asyncio.run(_main(sys.argv[1]))
