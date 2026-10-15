"""Что прислал пользователь: ссылку, пересланный пост канала или произвольный текст (ФТ-1.1)."""

import re
from dataclasses import dataclass

from app.models.enums import InputType

_URL = re.compile(r"https?://[^\s<>\"'«»]+", re.IGNORECASE)
MIN_TEXT_CHARS = 40
MAX_TEXT_CHARS = 5000


# Ссылки, которые не ведут на публикацию: приглашения в каналы и подписка. У Max пост —
# это max.ru/<канал>/<id>, и id буквенно-цифровой (AaFBz1MBFGw), а не числовой, как у
# Telegram; ссылка только на канал (max.ru/RT) — призыв подписаться из хвоста поста.
_NOT_A_PUBLICATION = re.compile(
    r"t\.me/(?:\+|joinchat/)|max\.ru/(?:join/|[^/?#\s]+/?(?:[?#]|$))", re.IGNORECASE
)
# Хвосты постов вида «Подписаться на RT: ТГ | Зеркало | MAX» — не часть новости.
_PROMO_LINE = re.compile(r"подпис(?:аться|ывайтесь)|наш канал|^[\W_]*$", re.IGNORECASE)


@dataclass(frozen=True)
class UserInput:
    type: InputType
    value: str  # URL публикации или текст
    # Для пересланного поста — его текст и время из метаданных пересылки: если сам пост
    # не открывается (канал скрыт в стране прокси), проверяем по ним.
    text: str | None = None
    published_at: str | None = None


def clean_post_text(text: str | None) -> str | None:
    lines = [line for line in (text or "").splitlines() if not _PROMO_LINE.search(line.strip())]
    cleaned = "\n".join(lines).strip()
    return cleaned or None


def extract_input(
    text: str | None,
    forward_channel: str | None = None,
    forward_message_id: int | None = None,
    forward_date: str | None = None,
) -> UserInput | None:
    """None — не поняли, что проверять (слишком короткий текст без ссылки)."""
    if forward_channel and forward_message_id:
        # Пересланный пост публичного канала: проверяем оригинал, а не копию в чате.
        return UserInput(
            InputType.TG_FORWARD,
            f"https://t.me/{forward_channel}/{forward_message_id}",
            text=clean_post_text(text),
            published_at=forward_date,
        )
    text = (text or "").strip()
    for match in _URL.finditer(text):
        url = match.group(0).rstrip(".,;:!?)]")
        if not _NOT_A_PUBLICATION.search(url):
            return UserInput(InputType.URL, url)
    if len(text) >= MIN_TEXT_CHARS:
        return UserInput(InputType.TEXT, text[:MAX_TEXT_CHARS])
    return None
