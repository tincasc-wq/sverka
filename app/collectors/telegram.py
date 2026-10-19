"""Сборщик публичных Telegram-каналов через веб-превью t.me/s/<канал>.

Не требует аккаунта и даёт последние ~20 постов канала. Сборщик на Telethon (служебный
аккаунт, полная история) — следующий шаг, когда будет номер для служебного аккаунта.
"""

from datetime import datetime

from selectolax.lexbor import LexborHTMLParser as HTMLParser

from app.collectors.store import DocumentIn


class PreviewDisabled(Exception):
    """У канала выключено веб-превью — нужен сборщик через Telethon."""


def channel_name(handle: str) -> str:
    return handle.strip().removeprefix("https://t.me/").removeprefix("@").strip("/")


def preview_url(handle: str) -> str:
    return f"https://t.me/s/{channel_name(handle)}"


def parse_channel_page(html: bytes | str, source_id: int | None) -> list[DocumentIn]:
    tree = HTMLParser(html)
    docs = []
    for message in tree.css("div.tgme_widget_message[data-post]"):
        post = message.attributes.get("data-post")  # "channel/123"
        text_node = message.css_first(".tgme_widget_message_text")
        text = text_node.text(separator="\n").strip() if text_node else ""
        if not post or not text:
            continue  # пост без текста (только медиа) нечего сверять
        links = [a.attributes.get("href") or "" for a in text_node.css("a[href]")]
        time_node = message.css_first("time[datetime]")
        published = datetime.fromisoformat(time_node.attributes["datetime"]) if time_node else None
        docs.append(
            DocumentIn(
                url=f"https://t.me/{post}",
                source_id=source_id,
                title=None,
                text=text,
                published_at=published,
                links=links,
            )
        )
    return docs


# Посты нового формата веб-превью не показывает («Please open Telegram to view this post»),
# но их текст есть в мета-описании страницы поста t.me/<канал>/<номер> (превью ссылки).
# РЕН ТВ 19.10.2026: проверка падала с «нет текста», сбор такие посты пропускал.
MAX_UNSUPPORTED_PER_PAGE = 5


def unsupported_posts(html: bytes | str) -> list[tuple[str, datetime | None]]:
    """Посты, которые веб-превью не показывает: (адрес поста, время)."""
    tree = HTMLParser(html)
    found = []
    for message in tree.css("div.tgme_widget_message[data-post]"):
        if message.css_first(".tgme_widget_message_text") is not None:
            continue
        if message.css_first(".message_media_not_supported") is None:
            continue
        post = message.attributes.get("data-post")
        time_node = message.css_first("time[datetime]")
        published = datetime.fromisoformat(time_node.attributes["datetime"]) if time_node else None
        if post:
            found.append((f"https://t.me/{post}", published))
    return found


def og_text(html: bytes | str) -> str | None:
    """Текст поста из мета-описания страницы t.me/<канал>/<номер>."""
    node = HTMLParser(html).css_first('meta[property="og:description"]')
    text = (node.attributes.get("content") or "").strip() if node else ""
    return text or None


async def fetch_unsupported(client, posts, source_id: int | None) -> list[DocumentIn]:
    """Дочитать тексты постов, которые веб-превью не показывает."""
    from app.collectors.http import fetch

    docs = []
    for url, published in posts[:MAX_UNSUPPORTED_PER_PAGE]:
        try:
            raw, _ = await fetch(client, url)
        except Exception:  # noqa: BLE001 — один пост не должен ронять сбор канала
            continue
        if text := og_text(raw):
            docs.append(DocumentIn(url=url, source_id=source_id, text=text, published_at=published))
    return docs
