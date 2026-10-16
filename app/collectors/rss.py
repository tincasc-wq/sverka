"""Сборщик RSS (ФТ-3.1): лента → новые публикации → полный текст статьи → архив."""

import asyncio
import calendar
import logging
import re
from datetime import UTC, datetime
from html import unescape

import feedparser
import httpx
import trafilatura
from selectolax.lexbor import LexborHTMLParser as HTMLParser

from app.collectors.http import fetch_bytes
from app.collectors.store import DocumentIn
from app.core.config import get_settings

log = logging.getLogger("sverka.rss")


def _entry_time(entry: dict) -> datetime | None:
    parsed = entry.get("published_parsed") or entry.get("updated_parsed")
    if not parsed:
        return None
    return datetime.fromtimestamp(calendar.timegm(parsed), tz=UTC)


def _strip_html(html: str) -> str:
    return HTMLParser(html).text(separator=" ").strip() if html else ""


def parse_feed(raw: bytes, source_id: int | None) -> list[DocumentIn]:
    feed = feedparser.parse(raw)
    if feed.bozo and not feed.entries:
        raise ValueError(f"unparseable feed: {feed.bozo_exception!r}")
    docs = []
    for entry in feed.entries:
        link = entry.get("link")
        if not link:
            continue
        docs.append(
            DocumentIn(
                url=link,
                source_id=source_id,
                title=(entry.get("title") or "").strip() or None,
                text=_strip_html(entry.get("summary", "")) or None,
                published_at=_entry_time(entry),
            )
        )
    return docs


_WORD = re.compile(r"[0-9A-Za-zА-Яа-яЁё]{4,}")
TITLE_MATCH_MIN = 0.3  # доля значимых слов заголовка, которые должны встретиться в начале текста


def _stems(text: str) -> set[str]:
    # Основа слова — первые 5 букв: «электростанцию» и «электростанция» совпадут.
    return {w.lower()[:5] for w in _WORD.findall(text)}


def text_matches_title(title: str | None, text: str | None) -> bool:
    """Текст со страницы про то же, что заголовок? Защита от чужой статьи на странице.

    ВЗГЛЯД 15.10.2026: заголовок «Росатом построит первую АЭС на Луне», а извлекатель взял
    со страницы соседний материал про интервью Зеленского — и публикация выпала из истории.
    Слишком короткий заголовок (меньше трёх значимых слов) не проверяем.
    """
    if not title or not text:
        return True
    wanted = _stems(title)
    if len(wanted) < 3:
        return True
    found = wanted & _stems(text[:2000])
    return len(found) / len(wanted) >= TITLE_MATCH_MIN


def extract_article_text(html: bytes | str, url: str) -> str | None:
    return trafilatura.extract(html, url=url, include_comments=False, include_tables=False)


_REF = re.compile(r'<ref target="([^"]+)"')


def extract_article_links(html: bytes | str, url: str) -> list[str]:
    """Ссылки из тела статьи (без меню и подвала) — по ним строятся рёбра explicit_link."""
    xml = trafilatura.extract(
        html, url=url, output_format="xml", include_links=True, include_comments=False
    )
    return [unescape(m) for m in _REF.findall(xml or "")]


async def enrich_full_text(client: httpx.AsyncClient, docs: list[DocumentIn]) -> None:
    """Подменяет аннотацию из ленты полным текстом статьи, если его удалось извлечь."""
    semaphore = asyncio.Semaphore(get_settings().collector_concurrency)

    async def one(doc: DocumentIn) -> None:
        async with semaphore:
            try:
                html = await fetch_bytes(client, doc.url)
            except (httpx.HTTPError, ValueError):
                return  # остаётся аннотация из ленты
            text = await asyncio.to_thread(extract_article_text, html, doc.url)
            if not text_matches_title(doc.title, text):
                log.info("чужой текст на странице %s — оставляем аннотацию из ленты", doc.url)
                return
            if text and len(text) > len(doc.text or ""):
                doc.text = text
                doc.links = await asyncio.to_thread(extract_article_links, html, doc.url)

    await asyncio.gather(*(one(d) for d in docs))
