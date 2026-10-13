"""Оповещения владельцу в Telegram от бота «Сверки» (ADMIN_TG_ID)."""

import logging

import httpx

from app.core.config import get_settings

log = logging.getLogger("sverka.notify")


async def notify_admin(text: str) -> bool:
    """True — сообщение доставлено. Ошибка оповещения не должна ломать действие пользователя."""
    settings = get_settings()
    if not settings.admin_tg_id or not settings.telegram_bot_token:
        log.warning("оповещение не отправлено (нет ADMIN_TG_ID или токена бота):\n%s", text)
        return False
    url = f"https://api.telegram.org/bot{settings.telegram_bot_token}/sendMessage"
    try:
        async with httpx.AsyncClient(timeout=15, proxy=settings.telegram_proxy or None) as client:
            response = await client.post(
                url,
                json={
                    "chat_id": settings.admin_tg_id,
                    "text": text,
                    "parse_mode": "HTML",
                    "disable_web_page_preview": True,
                },
            )
        return bool(response.json().get("ok"))
    except (httpx.HTTPError, ValueError) as exc:
        log.warning("оповещение не отправлено: %s", exc)
        return False
