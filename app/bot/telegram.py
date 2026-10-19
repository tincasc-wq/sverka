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


WATCH_DEFAULT = (6, 3)  # кнопка в боте: каждые 6 ч в течение 3 дней


SOON_RECHECK_HOURS = 1  # платные тарифы (со слежением): «Проверить через час»


def check_keyboard(check_id: int, pdf: bool = False, watch: bool = False) -> InlineKeyboardMarkup:
    hours = SOON_RECHECK_HOURS if watch else get_settings().recheck_hours
    label = "🔁 Проверить через час" if watch else f"🔁 Проверить снова через {hours:g} ч"
    rows = [
        [InlineKeyboardButton(text=label, callback_data=f"recheck:{check_id}:{hours:g}")],
        [InlineKeyboardButton(text="⚠️ Сообщить об ошибке", callback_data=f"report:{check_id}")],
    ]
    top = []
    if pdf:
        top.append(InlineKeyboardButton(text="📄 PDF", callback_data=f"pdf:{check_id}"))
    if watch:
        every, days = WATCH_DEFAULT
        top.append(
            InlineKeyboardButton(text=f"👁 Следить {days} дня", callback_data=f"watch:{check_id}")
        )
    if top:
        rows.insert(0, top)
    return InlineKeyboardMarkup(inline_keyboard=rows)


def watches_keyboard(watches) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=f"⏹ Остановить №{i}", callback_data=f"unwatch:{w.id}")]
            for i, w in enumerate(watches, 1)
        ]
    )
