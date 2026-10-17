"""Telegram-бот (этап 4): принимает ссылку, пересланный пост или текст и ставит проверку в очередь.

Саму проверку делает воркер (там модель эмбеддингов) и сам присылает ответ в чат —
бот отвечает «проверяю…» сразу (раздел 9: первый ответ < 2 с).

    python -m app.bot.main
"""

import asyncio
import logging
from datetime import timedelta
from html import escape

from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message, MessageOriginChannel
from arq import ArqRedis, create_pool
from arq.connections import RedisSettings

from app.bot import texts
from app.bot.inputs import extract_input
from app.bot.service import check_refusal, get_or_create_user
from app.bot.telegram import make_bot
from app.core.config import get_settings
from app.core.db import SessionLocal
from app.models import Check, Feedback
from app.models.enums import FeedbackStatus
from app.orgs.service import can_view_check, membership_of
from app.plans import can_export_pdf, has_priority, queue_options
from app.registry.service import find_source, source_card

log = logging.getLogger("sverka.bot")
router = Router()


class FeedbackForm(StatesGroup):
    text = State()


@router.message(CommandStart())
async def start(message: Message) -> None:
    base = get_settings().public_base_url.rstrip("/")
    await message.answer(
        texts.START + "\n\nПользуясь ботом, вы соглашаетесь с политикой обработки "
        f"персональных данных: {base}/privacy"
    )


@router.message(Command("help"))
async def help_(message: Message) -> None:
    await message.answer(texts.START)


@router.message(Command("tariff", "plan"))
async def tariff(message: Message) -> None:
    """Тариф и остаток проверок — лимит общий для бота и сайта."""
    from app.plans import get_plans, usage_of

    async with SessionLocal() as session:
        user = await get_or_create_user(session, message.from_user.id)
        await session.commit()
        usage = await usage_of(session, user)
    await message.answer(texts.format_tariff(usage, get_plans(), get_settings().public_base_url))


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


@router.callback_query(F.data.startswith("pdf:"))
async def send_pdf(callback: CallbackQuery) -> None:
    """Кнопка «📄 PDF»: бот сам присылает файл — без входа на сайт."""
    from aiogram.types import BufferedInputFile

    from app.web.report import build_pdf

    async with SessionLocal() as session:
        check = await session.get(Check, int(callback.data.split(":")[1]))
        user = await get_or_create_user(session, callback.from_user.id)
        await session.commit()
        membership = await membership_of(session, user)
        if check is None or not check.summary or not await can_view_check(session, check, user):
            await callback.answer("Отчёт не найден")
            return
        if not can_export_pdf(user, membership):
            await callback.answer("PDF-отчёт — в тарифе Pro. Подробнее: /tariff", show_alert=True)
            return
    await callback.answer("Готовлю PDF…")
    pdf = await build_pdf(check, membership)
    await callback.message.answer_document(
        BufferedInputFile(pdf, filename=f"sverka-{check.public_token[:8]}.pdf"),
        caption="Отчёт «Сверки» в PDF",
    )


@router.callback_query(F.data.startswith("watch:"))
async def watch_check(callback: CallbackQuery) -> None:
    """«👁 Следить»: перепроверять новость по расписанию и писать об изменениях."""
    from app.bot.telegram import WATCH_DEFAULT
    from app.watches import WatchRefused, start_watch

    every, days = WATCH_DEFAULT
    async with SessionLocal() as session:
        check = await session.get(Check, int(callback.data.split(":")[1]))
        user = await get_or_create_user(session, callback.from_user.id)
        await session.commit()
        try:
            if check is None:
                raise WatchRefused("Проверка не найдена.")
            watch = await start_watch(session, user, check, every, days)
        except WatchRefused as exc:
            await callback.answer(str(exc), show_alert=True)
            return
    await callback.answer()
    await callback.message.answer(
        texts.WATCH_STARTED.format(every=every, until=texts.msk(watch.until))
    )


@router.message(Command("watches"))
async def watches(message: Message) -> None:
    from app.bot.telegram import watches_keyboard
    from app.watches import user_watches

    async with SessionLocal() as session:
        user = await get_or_create_user(session, message.from_user.id)
        await session.commit()
        items = await user_watches(session, user)
    if not items:
        await message.answer(texts.NO_WATCHES)
        return
    await message.answer(texts.format_watches(items), reply_markup=watches_keyboard(items))


@router.callback_query(F.data.startswith("unwatch:"))
async def unwatch(callback: CallbackQuery) -> None:
    from app.watches import stop_watch

    async with SessionLocal() as session:
        user = await get_or_create_user(session, callback.from_user.id)
        stopped = await stop_watch(session, user, int(callback.data.split(":")[1]))
    await callback.answer("Слежение остановлено" if stopped else "Уже остановлено")


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


def _channel_post(message: Message) -> tuple[str | None, int | None, str | None]:
    origin = message.forward_origin
    if isinstance(origin, MessageOriginChannel) and origin.chat.username:
        return origin.chat.username, origin.message_id, origin.date.isoformat()
    return None, None, None


IMAGE_CAPTION_ENOUGH = 40  # подпись длиннее — проверяем её, картинку не распознаём


@router.message(Command("images"))
async def images_setting(message: Message) -> None:
    """/images — включить или выключить распознавание текста на картинках постов."""
    from app.plans import can_ocr

    async with SessionLocal() as session:
        user = await get_or_create_user(session, message.from_user.id)
        if not can_ocr(user, await membership_of(session, user)):
            await session.commit()
            await message.answer(texts.OCR_PAID)
            return
        user.ocr_images = not user.ocr_images
        await session.commit()
        await message.answer(texts.OCR_IMAGES_ON if user.ocr_images else texts.OCR_IMAGES_OFF)


@router.message(F.photo | F.document.mime_type.startswith("image/"))
async def check_image(message: Message, arq: ArqRedis) -> None:
    """Скриншот или пост-картинка: распознаём текст (тарифы с ocr) и проверяем его."""
    from app import ocr
    from app.plans import can_ocr

    caption = (message.caption or "").strip()
    channel, message_id, forward_date = _channel_post(message)
    async with SessionLocal() as session:
        user = await get_or_create_user(session, message.from_user.id)
        await session.commit()
        allowed = can_ocr(user, await membership_of(session, user))
    if len(caption) >= IMAGE_CAPTION_ENOUGH and (channel or not (allowed and user.ocr_images)):
        # Пост канала — проверим по ссылке (картинки распознает воркер, если включено /images);
        # без настройки — только подпись.
        await check(message, arq)
        return
    if not allowed:
        if channel:  # пересланный пост — проверим хотя бы по ссылке
            await check(message, arq)
        else:
            await message.answer(texts.OCR_PAID)
        return
    file = message.photo[-1] if message.photo else message.document
    if (file.file_size or 0) > ocr.MAX_BYTES:
        await message.answer(texts.OCR_FAILED.format(reason="картинка больше 8 МБ"))
        return
    await message.answer(texts.OCR_READING)
    buffer = await message.bot.download(file)
    try:
        recognized = await ocr.recognize_async(buffer.read())
    except ocr.OcrError as exc:
        await message.answer(texts.OCR_FAILED.format(reason=exc))
        return
    if len(recognized) < ocr.MIN_CHARS:
        await message.answer(texts.OCR_NO_TEXT)
        return
    from app.post_images import with_image_text

    body = with_image_text(caption, [recognized]) if caption else recognized
    shown = recognized if len(recognized) <= 400 else recognized[:399] + "…"
    await message.answer(texts.OCR_RECOGNIZED.format(text=escape(shown)))
    user_input = extract_input(body, channel, message_id, forward_date)
    if user_input is None:
        await message.answer(texts.NOT_UNDERSTOOD)
        return
    await _enqueue(message, arq, user_input)


@router.message(F.text | F.caption | F.forward_origin)
async def check(message: Message, arq: ArqRedis) -> None:
    channel, message_id, forward_date = _channel_post(message)
    user_input = extract_input(message.text or message.caption, channel, message_id, forward_date)
    if user_input is None:
        await message.answer(texts.NOT_UNDERSTOOD)
        return
    await _enqueue(message, arq, user_input)


async def _enqueue(message: Message, arq: ArqRedis, user_input) -> None:
    async with SessionLocal() as session:
        user = await get_or_create_user(session, message.from_user.id)
        await session.commit()
        if refusal := await check_refusal(session, user):
            await message.answer(refusal)
            return
        priority = has_priority(user, await membership_of(session, user))

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
        **queue_options(priority),
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
