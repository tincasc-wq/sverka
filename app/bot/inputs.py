"""Что прислал пользователь: ссылку, пересланный пост канала или произвольный текст (ФТ-1.1)."""

import re
from dataclasses import dataclass

from app.models.enums import InputType

_URL = re.compile(r"https?://[^\s<>\"'«»]+", re.IGNORECASE)
MIN_TEXT_CHARS = 40
MAX_TEXT_CHARS = 5000


@dataclass(frozen=True)
class UserInput:
    type: InputType
    value: str  # URL публикации или текст


def extract_input(
    text: str | None,
    forward_channel: str | None = None,
    forward_message_id: int | None = None,
) -> UserInput | None:
    """None — не поняли, что проверять (слишком короткий текст без ссылки)."""
    if forward_channel and forward_message_id:
        # Пересланный пост публичного канала: проверяем оригинал, а не копию в чате.
        return UserInput(
            InputType.TG_FORWARD, f"https://t.me/{forward_channel}/{forward_message_id}"
        )
    text = (text or "").strip()
    if match := _URL.search(text):
        return UserInput(InputType.URL, match.group(0).rstrip(".,;:!?)]"))
    if len(text) >= MIN_TEXT_CHARS:
        return UserInput(InputType.TEXT, text[:MAX_TEXT_CHARS])
    return None
