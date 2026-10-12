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
