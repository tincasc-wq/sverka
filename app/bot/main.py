"""Telegram-бот (этап 4): принимает ссылку, пересланный пост или текст и ставит проверку в очередь.

Саму проверку делает воркер (там модель эмбеддингов) и сам присылает ответ в чат —
бот отвечает «проверяю…» сразу (раздел 9: первый ответ < 2 с).

    python -m app.bot.main
"""

import asyncio
import logging
from datetime import timedelta

from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message, MessageOriginChannel
from arq import ArqRedis, create_pool
from arq.connections import RedisSettings

from app.bot import texts
from app.bot.inputs import extract_input
from app.bot.service import get_or_create_user, quota_exceeded
from app.bot.telegram import make_bot
from app.core.config import get_settings
from app.core.db import SessionLocal
from app.models import Check, Feedback
from app.models.enums import FeedbackStatus
from app.registry.service import find_source, source_card

log = logging.getLogger("sverka.bot")
router = Router()


class FeedbackForm(StatesGroup):
    text = State()


@router.message(CommandStart())
async def start(message: Message) -> None:
    await message.answer(texts.START)


@router.message(Command("help"))
async def help_(message: Message) -> None:
    await message.answer(texts.START)


@router.message(Command("source"))
async def source(message: Message, command: CommandObject) -> None:
    if not command.args:
        await message.answer(
            "Укажите источник: /source <code>tass.ru</code> или /source <code>@rbc_news</code>"
        )
        return
    async with SessionLocal() as session:
        found = await find_source(session, command.args)
        if found is None:
            await message.answer("Такого источника нет в реестре.")
            return
        await message.answer(texts.format_source_card(await source_card(session, found)))


@router.callback_query(F.data.startswith("recheck:"))
async def recheck(callback: CallbackQuery, arq: ArqRedis) -> None:
    check_id = int(callback.data.split(":")[1])
    async with SessionLocal() as session:
        check = await session.get(Check, check_id)
    if check is None:
        await callback.answer("Проверка не найдена")
        return
    hours = get_settings().recheck_hours
    await arq.enqueue_job(
        "bot_check",
        callback.message.chat.id,
        callback.message.message_id,
        check.input_raw,
        check.input_type,
        callback.from_user.id,
        False,  # без кэша: смысл в том, чтобы увидеть новые подтверждения
        _defer_by=timedelta(hours=hours),
    )
    await callback.answer()
    await callback.message.answer(texts.RECHECK_SCHEDULED.format(hours=hours))


@router.callback_query(F.data.startswith("report:"))
async def report_error(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(FeedbackForm.text)
    await state.update_data(check_id=int(callback.data.split(":")[1]))
    await callback.answer()
    await callback.message.answer(texts.FEEDBACK_ASK)


@router.message(FeedbackForm.text, F.text)
async def save_feedback(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    await state.clear()
    async with SessionLocal() as session:
        user = await get_or_create_user(session, message.from_user.id)
        session.add(
            Feedback(
                check_id=data["check_id"],
                user_id=user.id,
                text=message.text[:4000],
                status=FeedbackStatus.OPEN,
            )
        )
        await session.commit()
    await message.answer(texts.FEEDBACK_THANKS)


@router.message(F.text | F.caption | F.forward_origin)
async def check(message: Message, arq: ArqRedis) -> None:
    origin = message.forward_origin
    channel = message_id = forward_date = None
    if isinstance(origin, MessageOriginChannel) and origin.chat.username:
        channel, message_id = origin.chat.username, origin.message_id
        forward_date = origin.date.isoformat()
    user_input = extract_input(message.text or message.caption, channel, message_id, forward_date)
    if user_input is None:
        await message.answer(texts.NOT_UNDERSTOOD)
        return

    async with SessionLocal() as session:
        user = await get_or_create_user(session, message.from_user.id)
        await session.commit()
        if await quota_exceeded(session, user):
            await message.answer(texts.QUOTA.format(limit=user.daily_quota))
            return

    await message.answer(texts.CHECKING)
    await arq.enqueue_job(
        "bot_check",
        message.chat.id,
        message.message_id,
        user_input.value,
        user_input.type.value,
        message.from_user.id,
        True,
        user_input.text,
        user_input.published_at,
    )


async def run() -> None:
    settings = get_settings()
    if not settings.telegram_bot_token:
        log.warning("TELEGRAM_BOT_TOKEN не задан — бот не запущен (впишите токен в .env)")
        await asyncio.Event().wait()  # не падать в цикл перезапусков контейнера
    bot: Bot = make_bot()
    arq = await create_pool(RedisSettings.from_dsn(settings.redis_url))
    dispatcher = Dispatcher(arq=arq)
    dispatcher.include_router(router)
    me = await bot.get_me()
    log.info("бот @%s запущен", me.username)
    try:
        await dispatcher.start_polling(bot)
    finally:
        await arq.aclose()
        await bot.session.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s: %(message)s")
    asyncio.run(run())
