"""Клиент Bot API: общий для процесса бота и воркера (воркер присылает готовые отчёты)."""

from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.enums import ParseMode
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from app.core.config import get_settings


def make_bot() -> Bot:
    settings = get_settings()
    # api.telegram.org из России напрямую недоступен: тот же прокси, что и для сбора каналов.
    session = AiohttpSession(proxy=settings.telegram_proxy) if settings.telegram_proxy else None
    return Bot(
        settings.telegram_bot_token,
        session=session,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML, link_preview_is_disabled=True),
    )


def check_keyboard(check_id: int, pdf: bool = False) -> InlineKeyboardMarkup:
    hours = get_settings().recheck_hours
    rows = [
        [
            InlineKeyboardButton(
                text=f"🔁 Проверить снова через {hours:g} ч",
                callback_data=f"recheck:{check_id}",
            )
        ],
        [InlineKeyboardButton(text="⚠️ Сообщить об ошибке", callback_data=f"report:{check_id}")],
    ]
    if pdf:
        rows.insert(0, [InlineKeyboardButton(text="📄 PDF", callback_data=f"pdf:{check_id}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)
