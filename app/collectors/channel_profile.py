"""Описание Telegram-канала, которого нет в реестре: пародийный ли он и не подражает ли чужому.

«Раньше всех? Ну почти.» (@vas_naebali_news) прямо пишет в описании «Все что написано здесь
неправда… Пародийный канал» и копирует название «Раньше всех. Ну почти» (@bbbreaking).
Без этого его выдумка шла как «слишком рано: независимых сообщений ещё нет» (18.10.2026).
"""

import logging
import re
import time
from dataclasses import dataclass

from selectolax.lexbor import LexborHTMLParser as HTMLParser

from app.collectors.http import fetch, make_client
from app.core.config import get_settings

log = logging.getLogger("sverka.channels")

TTL_SECONDS = 12 * 3600
_cache: dict[str, tuple[float, "ChannelProfile | None"]] = {}

PARODY = re.compile(
    r"пароди\w*|сатир\w*|юмор\w*|шуточн\w*|стёб\w*|"
    r"(?:всё|все)[,\s]+что\s+(?:(?:мы|здесь|тут)\s+)*(?:написано|пишем|публикуем)[^.]{0,30}?(?:неправда|ложь|выдумк\w*|вымысел)|"
    r"не\s+является\s+(?:правдой|достоверн\w*)|вымышленн\w*\s+новост\w*|фейков\w*\s+новост\w*|"
    r"\bparod\w*|\bsatir\w*|\bhumou?r\w*|fake\s+news|not\s+real\s+news",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class ChannelProfile:
    handle: str  # без @
    title: str
    description: str


def parse_profile(handle: str, html: bytes | str) -> ChannelProfile | None:
    tree = HTMLParser(html)
    title = tree.css_first(".tgme_page_title")
    if title is None:
        return None
    description = tree.css_first(".tgme_page_description")
    return ChannelProfile(
        handle=handle,
        title=" ".join(title.text().split()),
        description=" ".join(description.text(separator=" ").split()) if description else "",
    )


async def channel_profile(handle: str) -> ChannelProfile | None:
    handle = handle.lstrip("@").lower()
    cached = _cache.get(handle)
    if cached and time.monotonic() - cached[0] < TTL_SECONDS:
        return cached[1]
    settings = get_settings()
    try:
        async with make_client(
            proxy=settings.telegram_proxy, public_only=True, ipv6=settings.telegram_ipv6
        ) as client:
            raw, _ = await fetch(client, f"https://t.me/{handle}")
        profile = parse_profile(handle, raw)
    except Exception:  # noqa: BLE001 — без описания проверка всё равно идёт
        log.warning("описание канала @%s не получено", handle, exc_info=True)
        return None
    _cache[handle] = (time.monotonic(), profile)
    return profile


def parody_marker(profile: ChannelProfile) -> str | None:
    """Фраза из описания (или названия), по которой видно, что канал — пародия или сатира."""
    for text in (profile.description, profile.title):
        match = PARODY.search(text or "")
        if match and len(text) <= 200:
            return text  # короткое описание — цитируем целиком, так понятнее
        if match:
            start = max(0, text.rfind(".", 0, match.start()) + 1)
            end = text.find(".", match.end())
            phrase = text[start : end + 1 if end != -1 else len(text)].strip()
            return phrase if len(phrase) <= 160 else phrase[:159] + "…"
    return None


def _norm(name: str) -> str:
    return "".join(ch for ch in name.lower().replace("ё", "е") if ch.isalnum())


def imitated(profile: ChannelProfile, sources) -> str | None:
    """Название канала совпадает с источником из реестра, а адрес — другой: «Раньше всех? Ну
    почти.» (@vas_naebali_news) против «Раньше всех. Ну почти» (@bbbreaking)."""
    title = _norm(profile.title)
    if len(title) < 6:
        return None
    for s in sources:
        names = (s.name, *s.aliases)
        if (
            any(_norm(n) == title for n in names if n)
            and s.handle_or_domain.lstrip("@").lower() != profile.handle
        ):
            return f"{s.name} ({s.handle_or_domain})"
    return None
