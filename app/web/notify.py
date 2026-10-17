"""Оповещения владельцу в Telegram от бота «Сверки» (ADMIN_TG_ID)."""

import logging

import httpx

from app.core.config import get_settings

log = logging.getLogger("sverka.notify")


async def send_telegram(chat_id: int, text: str) -> bool:
    """Сообщение от бота «Сверки» через Bot API — из веб-процесса, где нет объекта бота."""
    settings = get_settings()
    if not settings.telegram_bot_token:
        log.warning("сообщение в Telegram не отправлено (нет токена бота):\n%s", text)
        return False
    url = f"https://api.telegram.org/bot{settings.telegram_bot_token}/sendMessage"
    try:
        async with httpx.AsyncClient(timeout=15, proxy=settings.telegram_proxy or None) as client:
            response = await client.post(
                url,
                json={
                    "chat_id": chat_id,
                    "text": text,
                    "parse_mode": "HTML",
                    "disable_web_page_preview": True,
                },
            )
        return bool(response.json().get("ok"))
    except (httpx.HTTPError, ValueError) as exc:
        log.warning("сообщение в Telegram не отправлено: %s", exc)
        return False


class BotApi:
    """Заменитель aiogram.Bot для app.watches.notify в веб-процессе."""

    async def send_message(self, chat_id: int, text: str, **_) -> None:
        if not await send_telegram(chat_id, text):
            raise RuntimeError("telegram send failed")


async def notify_admin(text: str) -> bool:
    """True — сообщение доставлено. Ошибка оповещения не должна ломать действие пользователя."""
    settings = get_settings()
    if not settings.admin_tg_id:
        log.warning("оповещение не отправлено (нет ADMIN_TG_ID):\n%s", text)
        return False
    return await send_telegram(settings.admin_tg_id, text)
