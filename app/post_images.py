"""Текст с картинок поста: «Вот документ:» + скан указа, скриншот чужой публикации.

По настройке пользователя («распознать текст на картинках поста», тарифы с ocr) при проверке
поста в Telegram скачиваем до трёх его картинок, распознаём и дописываем текст к посту с
пометкой. Пост в архиве один на всех — пометка не даёт распознавать его повторно.
"""

import logging
import re

from app.collectors.http import fetch, make_client
from app.collectors.urls import domain_of
from app.core.config import get_settings
from app.models import Document

log = logging.getLogger("sverka.ocr")

MARK = "[Текст с изображения]"
MAX_IMAGES = 3
_PHOTO = re.compile(
    r"tgme_widget_message_photo_wrap[^>]*?background-image:url\('([^']+)'\)", re.IGNORECASE
)


def telegram_image_urls(html: str) -> list[str]:
    return list(dict.fromkeys(_PHOTO.findall(html)))[:MAX_IMAGES]


def with_image_text(text: str | None, recognized: list[str]) -> str:
    parts = [p for p in recognized if p]
    if not parts:
        return text or ""
    return "\n\n".join(x for x in (text or "", MARK, "\n\n".join(parts)) if x)


async def add_image_text(doc: Document) -> bool:
    """True — к посту добавлен текст с картинок (вектор надо пересчитать)."""
    from app import ocr

    if domain_of(doc.url) != "t.me" or MARK in (doc.text or "") or not ocr.available():
        return False
    settings = get_settings()
    try:
        async with make_client(
            proxy=settings.telegram_proxy, public_only=True, ipv6=settings.telegram_ipv6
        ) as client:
            page, _ = await fetch(client, f"{doc.url}?embed=1")
            recognized = []
            for url in telegram_image_urls(page.decode("utf-8", errors="replace")):
                image, _ = await fetch(client, url)
                try:
                    text = await ocr.recognize_async(image)
                except ocr.OcrError:
                    continue
                if len(text) >= ocr.MIN_CHARS:
                    recognized.append(text)
    except Exception:  # noqa: BLE001 — без картинок проверка всё равно идёт по тексту поста
        log.warning("картинки поста %s не распознаны", doc.url, exc_info=True)
        return False
    if not recognized:
        return False
    doc.text = with_image_text(doc.text, recognized)
    doc.embedding = None
    return True
