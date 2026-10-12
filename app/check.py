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
from zoneinfo import ZoneInfo

import httpx
import trafilatura
from selectolax.lexbor import LexborHTMLParser as HTMLParser
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.collectors import rss, telegram
from app.collectors.fingerprint import content_hash
from app.collectors.http import fetch, make_client
from app.collectors.store import DocumentIn, SaveResult, save_document
from app.collectors.urls import domain_of, normalize_url, without_query
from app.core.config import get_settings
from app.core.db import SessionLocal
from app.core.scoring import get_scoring
from app.embeddings import Embedder, document_text
from app.llm import LlmClient, LlmUnavailable
from app.models import Check, Document, Source
from app.models.enums import CheckStatus, InputType
from app.pipeline.analysis import analyze, make_judge
from app.pipeline.report import build_report
from app.pipeline.story import build_story

MSK = ZoneInfo("Europe/Moscow")  # российские сайты без часового пояса пишут московское время


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
    return DocumentIn(
        url=url,
        source_id=source_id,
        title=meta.title if meta else None,
        text=rss.extract_article_text(raw, url),
        published_at=published_time(raw),
        links=rss.extract_article_links(raw, url),
    )


_TIME_META = (
    'meta[property="article:published_time"]',
    'meta[name="article:published_time"]',
    'meta[itemprop="datePublished"]',
    'meta[property="og:published_time"]',
)


def published_time(html: bytes | str) -> datetime | None:
    """Заявленное время публикации — только если в разметке есть время, а не одна дата.

    Дата без времени превратилась бы в полночь и сделала статью ложно «самой ранней».
    """
    tree = HTMLParser(html)
    for selector in _TIME_META:
        node = tree.css_first(selector)
        value = (node.attributes.get("content") or "").strip() if node else ""
        if "T" not in value:
            continue
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            continue
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=MSK)
    return None


async def resolve_seed(
    session: AsyncSession, arg: str, fallback: DocumentIn | None = None
) -> Document:
    if arg.startswith("doc:"):
        doc = await session.get(Document, int(arg.removeprefix("doc:")))
        if doc is None:
            raise ValueError(f"no document {arg}")
        return doc
    url = normalize_url(arg)
    # Сначала точный адрес, потом тот же путь без параметров: в ссылках из соцсетей и
    # рекомендательных виджетов бывают метки, которых нет в нашем списке.
    for candidate in dict.fromkeys((url, without_query(url))):
        doc = await session.scalar(select(Document).where(Document.url == candidate))
        if doc is not None:
            return doc
    source_id = await _source_for(session, url)
    try:
        incoming = await _download(arg, source_id)
    except (httpx.HTTPError, ValueError):
        if fallback is None or not fallback.text:
            raise
        # Пост не открылся (например, канал скрыт в стране прокси), но его текст и время
        # пришли вместе с пересылкой — проверяем по ним.
        incoming = fallback
        incoming.url = url
        incoming.source_id = source_id
    result = await save_document(session, incoming)
    if result == SaveResult.DUPLICATE:
        # Тот же текст у того же источника уже в архиве под другим адресом — берём его.
        body = "\n".join(part for part in (incoming.title, incoming.text) if part)
        return await session.scalar(
            select(Document).where(
                Document.source_id == incoming.source_id,
                Document.content_hash == content_hash(body),
            )
        )
    return await session.scalar(select(Document).where(Document.url == url))


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
    fallback: DocumentIn | None = None,
    llm: LlmClient | None = None,
) -> Check:
    seed = await resolve_seed(session, arg, fallback)
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

    # Проверка создаётся до LLM-вызовов: к ней привязывается учёт расходов (llm_calls).
    check = Check(
        user_id=user_id,
        input_type=input_type,
        input_raw=arg,
        document_id=seed.id,
        status=CheckStatus.RUNNING,
        public_token=secrets.token_urlsafe(12),
    )
    session.add(check)
    await session.flush()

    scoring = get_scoring()
    use_llm = llm is not None and llm.available()
    judge = make_judge(llm, session, check.id) if use_llm else None
    result = await build_story(session, seed, judge=judge)
    report = build_report(result, scoring.graph)
    if use_llm:
        try:
            report["analysis"] = await analyze(session, llm, result, check.id, scoring)
            report["note"] = None
            check.confirmation_index = report["analysis"]["index"]
        except LlmUnavailable as exc:
            report["analysis"] = {"error": f"LLM недоступна: {exc}"[:300]}

    check.story_id = result.story_id
    check.status = CheckStatus.DONE
    check.summary = json.dumps(report, ensure_ascii=False)
    check.finished_at = datetime.now(UTC)
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
        check = await run_check(session, arg, _embedder(), llm=LlmClient())
    print(json.dumps(json.loads(check.summary), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print(__doc__)
        sys.exit(1)
    asyncio.run(_main(sys.argv[1]))
