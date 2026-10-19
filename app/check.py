"""Проверка публикации из консоли (этап 3, детерминированная часть).

  python -m app.check <url>       # публикация по ссылке (скачается, если её нет в архиве)
  python -m app.check doc:<id>    # публикация из архива по id

Если у публикации нет эмбеддинга, команду нужно запускать в контейнере worker (там модель).
"""

import asyncio
import hashlib
import json
import re
import secrets
import sys
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import httpx
import trafilatura
from selectolax.lexbor import LexborHTMLParser as HTMLParser
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.collectors import maxru, rss, telegram
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
from app.pipeline.graph import USER_TEXT_PREFIX
from app.pipeline.report import build_report
from app.pipeline.story import build_story, story_headline

MSK = ZoneInfo("Europe/Moscow")
SATIRE_STATUS = {
    "satire_origin": "похоже на шутку: первоисточник — {source}",
    "satire_seed": "это материал сатирического издания ({source}): вымысел по замыслу",
}  # российские сайты без часового пояса пишут московское время


async def _source_for(session: AsyncSession, url: str) -> int | None:
    if domain_of(url) == "t.me":
        handle = "@" + url.split("/")[3]
    elif domain_of(url) == maxru.MAX_HOST:
        handle = maxru.channel_handle(url)
    else:
        handle = domain_of(url)
    return await session.scalar(select(Source.id).where(Source.handle_or_domain == handle))


async def _download(url: str, source_id: int | None) -> DocumentIn:
    """Скачивает публикацию, которой ещё нет в архиве."""
    if domain_of(url) == "t.me":
        proxy = get_settings().telegram_proxy
        ipv6 = get_settings().telegram_ipv6
        async with make_client(proxy=proxy, public_only=True, ipv6=ipv6) as client:
            raw, _ = await fetch(client, f"{url}?embed=1")
        docs = telegram.parse_channel_page(raw, source_id)
        if not docs and (unsupported := telegram.unsupported_posts(raw)):
            # Пост нового формата: веб-превью его не показывает, текст — в превью ссылки.
            async with make_client(proxy=proxy, public_only=True, ipv6=ipv6) as client:
                docs = await telegram.fetch_unsupported(
                    client, [(url, unsupported[0][1])], source_id
                )
        if not docs:
            raise ValueError(f"no text found in Telegram post {url}")
        docs[0].url = url
        return docs[0]

    if maxru.is_max_post(url):
        return await _download_max(url, source_id)

    async with make_client(public_only=True) as client:
        raw, _ = await fetch(client, url)
    meta = trafilatura.extract_metadata(raw)
    title = meta.title if meta else None
    text = rss.extract_article_text(raw, url)
    if not rss.text_matches_title(title, text):
        # Со страницы извлеклась соседняя статья — берём описание страницы, а не чужой текст.
        text = (meta.description if meta else None) or None
    return DocumentIn(
        url=url,
        source_id=source_id,
        title=title,
        text=text,
        published_at=published_time(raw),
        stated_date=stated_date(raw),
        lang=page_lang(raw),
        links=rss.extract_article_links(raw, url),
    )


async def _download_max(url: str, source_id: int | None) -> DocumentIn:
    """Пост Max: напрямую, а если Max ограничил наш адрес и отдал заглушку — через шлюз."""
    async with make_client(public_only=True) as client:
        raw, _ = await fetch(client, url)
    try:
        return maxru.parse_post(raw, url, source_id)
    except maxru.MaxStub:
        proxy = get_settings().collector_proxy
        if not proxy:
            raise
    async with make_client(proxy=proxy) as client:
        raw, _ = await fetch(client, url)
    return maxru.parse_post(raw, url, source_id)


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


_MONTHS_EN = {
    m: i
    for i, m in enumerate(
        (
            "january",
            "february",
            "march",
            "april",
            "may",
            "june",
            "july",
            "august",
            "september",
            "october",
            "november",
            "december",
        ),
        1,
    )
}
_MONTHS_RU = {
    m: i
    for i, m in enumerate(
        (
            "января",
            "февраля",
            "марта",
            "апреля",
            "мая",
            "июня",
            "июля",
            "августа",
            "сентября",
            "октября",
            "ноября",
            "декабря",
        ),
        1,
    )
}
_ISO_DATE = re.compile(r"(20\d{2})-(\d{2})-(\d{2})")
_EN_DATE = re.compile(r"\b(" + "|".join(_MONTHS_EN) + r")\s+(\d{1,2}),\s+(20\d{2})\b", re.I)
_RU_DATE = re.compile(r"\b(\d{1,2})\s+(" + "|".join(_MONTHS_RU) + r")\s+(20\d{2})\b", re.I)


def stated_date(html: bytes | str) -> date | None:
    """Дата публикации без времени: из разметки, а если её нет — из видимого текста.

    Из текста берём самую позднюю дату не в будущем: даты в самой статье обычно раньше
    публикации, так что ошибиться можно только в сторону «свежее», а не «старее».
    """
    tree = HTMLParser(html)
    today = datetime.now(MSK).date()

    def valid(y: int, m: int, d: int) -> date | None:
        try:
            value = date(y, m, d)
        except ValueError:
            return None
        return value if value <= today else None

    for selector in (*_TIME_META, "time[datetime]"):
        node = tree.css_first(selector)
        value = (
            (node.attributes.get("content") or node.attributes.get("datetime") or "")
            if node
            else ""
        )
        if match := _ISO_DATE.match(value.strip()):
            if found := valid(*map(int, match.groups())):
                return found
    body = tree.body.text(separator=" ") if tree.body else ""
    found = [valid(int(y), _MONTHS_EN[m.lower()], int(d)) for m, d, y in _EN_DATE.findall(body)]
    found += [valid(int(y), _MONTHS_RU[m.lower()], int(d)) for d, m, y in _RU_DATE.findall(body)]
    return max((x for x in found if x), default=None)


def page_lang(html: bytes | str) -> str | None:
    """Язык страницы из <html lang>: для сайтов не из реестра (у тех язык — в реестре)."""
    node = HTMLParser(html).css_first("html")
    value = (node.attributes.get("lang") or "").strip().lower() if node else ""
    return value[:2] if len(value) >= 2 and value[:2].isalpha() else "ru"


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


TEXT_ANCHOR_DAYS = 30
TEXT_ANCHOR_MIN_SIM = 0.6


async def text_seed(session: AsyncSession, text: str, embedder: Embedder) -> Document:
    """Произвольный текст → временная «публикация» для полной проверки, как у ссылки.

    Время ставится по самым близким по смыслу статьям за 30 дней — так окно поиска истории
    (±72 ч) накрывает само событие, даже если текст прислали через несколько дней.
    """
    settings = get_settings()
    body = " ".join(text.split())
    url = USER_TEXT_PREFIX + hashlib.sha256(body.encode()).hexdigest()[:32]
    [vector] = await asyncio.to_thread(embedder.embed, [body[: settings.embedding_max_chars]])
    moment = func.coalesce(Document.published_at, Document.first_seen_at)
    distance = Document.embedding.cosine_distance(vector)
    nearest = list(
        await session.scalars(
            select(moment)
            .where(
                Document.embedding.is_not(None),
                ~Document.url.startswith(USER_TEXT_PREFIX),
                Document.first_seen_at >= datetime.now(UTC) - timedelta(days=TEXT_ANCHOR_DAYS),
                distance <= 1 - TEXT_ANCHOR_MIN_SIM,
            )
            .order_by(distance)
            .limit(5)
        )
    )
    # Чуть позже самой поздней из близких статей: текст пересказывает уже вышедшее.
    anchor = max(nearest) + timedelta(minutes=1) if nearest else datetime.now(UTC)
    doc = await session.scalar(select(Document).where(Document.url == url))
    if doc is None:
        doc = Document(url=url, text=body, embedding=vector, published_at=anchor)
        session.add(doc)
    else:
        doc.embedding, doc.published_at = vector, anchor
    await session.flush()
    return doc


async def recent_check(
    session: AsyncSession, document_id: int, hours: float, org_id: int | None = None
) -> Check | None:
    """Готовый отчёт по той же публикации моложе hours часов (ФТ-1.3).

    Отчёты организаций в кэш для других не идут: по ним видно, что интересует клиента.
    """
    since = datetime.now(UTC) - timedelta(hours=hours)
    return await session.scalar(
        select(Check)
        .where(
            Check.document_id == document_id,
            Check.status == CheckStatus.DONE,
            Check.finished_at >= since,
            Check.org_id.is_(None) | (Check.org_id == org_id),
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
    existing: Check | None = None,
    org_id: int | None = None,
    channel: str | None = None,
    input_raw: str | None = None,
    ocr_images: bool = False,
    ocr_rare_langs: bool = False,
) -> Check:
    """existing — уже созданная запись проверки (сайт создаёт её сразу, чтобы показать ожидание).

    ocr_images — распознать текст на картинках поста (настройка пользователя, тарифы с ocr).
    """
    seed = await resolve_seed(session, arg, fallback)
    if existing is not None:
        org_id = existing.org_id
    if ocr_images:
        from app.post_images import add_image_text

        await add_image_text(seed, ocr_rare_langs)
        cache_hours = 0  # прошлая проверка могла быть без текста с картинок
    if cache_hours and (cached := await recent_check(session, seed.id, cache_hours, org_id)):
        if existing is not None:
            existing.document_id = seed.id
            existing.story_id = cached.story_id
            existing.summary = cached.summary
            existing.confirmation_index = cached.confirmation_index
            existing.status = CheckStatus.DONE
            existing.finished_at = datetime.now(UTC)
            cached = existing
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
    if existing is not None:
        check = existing
        check.document_id = seed.id
        check.status = CheckStatus.RUNNING
    else:
        check = Check(
            user_id=user_id,
            input_type=input_type,
            input_raw=input_raw or arg,
            document_id=seed.id,
            status=CheckStatus.RUNNING,
            public_token=secrets.token_urlsafe(12),
            org_id=org_id,
            channel=channel or ("service" if user_id is None else None),
        )
        session.add(check)
    await session.flush()

    scoring = get_scoring()
    use_llm = llm is not None and llm.available()
    judge = make_judge(llm, session, check.id) if use_llm else None
    headline_vector = None
    if judge is not None and embedder is not None and (headline := story_headline(seed)):
        [headline_vector] = await asyncio.to_thread(embedder.embed, [headline])
    result = await build_story(session, seed, judge=judge, headline_vector=headline_vector)
    report = build_report(result, scoring.graph)
    await check_unregistered_channels(result, report)
    satire = report.get("satire")
    if satire:
        # Первоисточник или сама публикация — сатира: сверять вымысел незачем, индекс не считаем.
        status = "satire_origin" if satire["origin"] else "satire_seed"
        report["analysis"] = {
            "index": None,
            "status": status,
            "status_text": SATIRE_STATUS[status].format(source=satire["source"]),
            "claims": [],
            "verified_documents": 0,
            "rejected_quotes": 0,
            "low_confidence": 0,
            "cost_rub": 0.0,
        }
        report["note"] = None
    elif use_llm:
        try:
            report["analysis"] = await analyze(session, llm, result, check.id, scoring)
            retelling_outside_archive(report)
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


async def check_unregistered_channels(result, report: dict) -> None:
    """Канал не из реестра: описание «пародийный канал» → сатира; чужое название → предупреждение.

    Смотрим первоисточник истории и саму присланную публикацию (если это посты Telegram).
    """
    from app.collectors.channel_profile import channel_profile, imitated, parody_marker

    nodes = {n.id: n for n in result.nodes}
    seed = nodes.get(result.seed.id)
    primary = nodes.get(result.graph.primary_id) if result.graph.primary_id else None
    seen: set[str] = set()
    for node in (primary, seed):
        if node is None or node.source is not None or domain_of(node.url) != "t.me":
            continue
        handle = node.url.split("/")[3].lower() if len(node.url.split("/")) > 3 else ""
        if not handle or handle in seen:
            continue
        seen.add(handle)
        profile = await channel_profile(handle)
        if profile is None:
            continue
        copies = imitated(profile, result.sources.values())
        if copies and node is seed:
            report["imitates"] = {"channel": f"{profile.title} (@{handle})", "original": copies}
        marker = parody_marker(profile)
        if marker and not report.get("satire"):
            is_origin = node is primary and (seed is None or primary.id != seed.id)
            report["satire"] = {
                "source": f"{profile.title} (@{handle})",
                "url": node.url,
                "origin": is_origin,
                "seed": node is seed or not is_origin,
                "reason": marker,
            }


def retelling_outside_archive(report: dict) -> None:
    """«Рано судить» неверно, если публикация сама пересказывает чужую, которой у нас нет.

    Пост УНИАН «…, — Futurism» вышел полчаса назад, но история старше: её первоисточник
    просто не в архиве. Так и пишем, вместо «слишком рано: независимых сообщений ещё нет».
    """
    analysis = report.get("analysis") or {}
    seed = next((t for t in report.get("timeline", []) if t.get("is_seed")), None)
    cites = (seed or {}).get("attributes_outside_archive") or []
    alone = report.get("core_publications", report.get("publications", 0)) <= 1
    if not cites or analysis.get("status") not in ("too_early", "no_independent", "not_confirmed"):
        return
    if analysis["status"] != "too_early" and not alone:
        return
    analysis["status"] = "retelling_outside"
    analysis["status_text"] = (
        f"пересказ публикации {', '.join(cites)}: её нет в архиве «Сверки», "
        "подтвердить по архиву нельзя"
    )


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
