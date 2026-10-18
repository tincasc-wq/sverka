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


_SITEMAP_NS = {
    "s": "http://www.sitemaps.org/schemas/sitemap/0.9",
    "n": "http://www.google.com/schemas/sitemap-news/0.9",
}


def is_news_sitemap(raw: bytes) -> bool:
    head = raw[:2000]
    return b"<urlset" in head and b"sitemap-news" in head


def parse_news_sitemap(raw: bytes, source_id: int | None) -> list[DocumentIn]:
    """Карта сайта для Google News: у кого нет RSS (CNN закрыл ленты в 2022–2024), есть она —
    свежие материалы за двое суток с заголовком и временем публикации. Текст докачивается
    так же, как для RSS (enrich_full_text)."""
    import xml.etree.ElementTree as ET

    try:
        root = ET.fromstring(raw)
    except ET.ParseError as exc:
        raise ValueError(f"unparseable news sitemap: {exc}") from exc
    docs = []
    for url in root.findall("s:url", _SITEMAP_NS):
        loc = (url.findtext("s:loc", default="", namespaces=_SITEMAP_NS) or "").strip()
        title = url.findtext("n:news/n:title", default="", namespaces=_SITEMAP_NS).strip()
        when = url.findtext("n:news/n:publication_date", default="", namespaces=_SITEMAP_NS)
        if not loc:
            continue
        try:
            published = (
                datetime.fromisoformat(when.strip().replace("Z", "+00:00")) if when else None
            )
        except ValueError:
            published = None
        if published is not None and published.tzinfo is None:
            published = published.replace(tzinfo=UTC)
        docs.append(
            DocumentIn(url=loc, source_id=source_id, title=title or None, published_at=published)
        )
    return docs


def parse_feed(raw: bytes, source_id: int | None) -> list[DocumentIn]:
    if is_news_sitemap(raw):
        return parse_news_sitemap(raw, source_id)
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
