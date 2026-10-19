"""Проверки, запущенные с сайта: запись создаётся сразу (страница ожидания), работа — в воркере."""

import json
import secrets
from datetime import UTC, datetime

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot import texts
from app.bot.inputs import extract_input
from app.bot.service import check_refusal
from app.check import run_check, text_seed
from app.collectors.http import UnsafeUrl
from app.collectors.maxru import MaxStub
from app.core.config import get_settings
from app.embeddings import Embedder
from app.llm import LlmClient
from app.models import Check, User
from app.models.enums import CheckStatus, InputType
from app.orgs.service import membership_of

NOT_UNDERSTOOD = (
    "Не понял, что проверять. Вставьте ссылку на публикацию или текст новости "
    "(от нескольких предложений)."
)


MAX_STUB_REASON = (
    "Max не отдал пост: ссылка неверна, пост удалён или Max временно ограничил доступ. "
    "Попробуйте через несколько минут или пришлите текст поста."
)


class CheckRefused(Exception):
    """Проверку не запускаем: непонятный ввод или исчерпан лимит."""


async def create_web_check(
    session: AsyncSession, user: User, value: str, channel: str = "web"
) -> Check:
    user_input = extract_input(value)
    if user_input is None:
        raise CheckRefused(NOT_UNDERSTOOD)
    if refusal := await check_refusal(session, user):
        if user.tg_id is None and user.email is None and "в сутки" in refusal:
            refusal += " Войдите по почте или через Telegram — лимит станет больше."
        raise CheckRefused(refusal)
    membership = await membership_of(session, user)
    check = Check(
        channel=channel,
        user_id=user.id,
        org_id=membership.org.id if membership else None,
        input_type=user_input.type,
        input_raw=user_input.value,
        status=CheckStatus.QUEUED,
        public_token=secrets.token_urlsafe(12),
    )
    session.add(check)
    await session.commit()
    return check


BOT_WALL_REASON = (
    "сайт закрыт от автоматических запросов (защита от роботов). Скопируйте текст статьи "
    "и вставьте его в поле проверки — проверим так же полно."
)


def _failure_reason(exc: Exception) -> str:
    from app.check import BotProtected

    if isinstance(exc, MaxStub):
        return MAX_STUB_REASON
    if isinstance(exc, BotProtected):
        return BOT_WALL_REASON
    if isinstance(exc, UnsafeUrl):
        return "ссылка ведёт на недоступный адрес."
    if isinstance(exc, httpx.HTTPStatusError):
        return f"сайт ответил ошибкой {exc.response.status_code}."
    return "страница не открылась или на ней нет текста."


async def run_web_check(
    session: AsyncSession,
    check_id: int,
    embedder: Embedder,
    llm: LlmClient | None,
    *,
    use_cache: bool = True,
) -> Check:
    settings = get_settings()
    check = await session.get(Check, check_id)
    if llm is not None and check.org_id is not None:
        from app.models import Organization

        org = await session.get(Organization, check.org_id)
        llm = llm.restricted(org.ru_only)
    ocr_images = ocr_rare_langs = False
    if check.user_id is not None:
        from app.plans import can_ocr

        user = await session.get(User, check.user_id)
        allowed = bool(user and can_ocr(user, await membership_of(session, user)))
        ocr_images = allowed and user.ocr_images
        ocr_rare_langs = allowed and user.ocr_rare_langs
    check.status = CheckStatus.RUNNING
    await session.commit()
    try:
        arg = check.input_raw
        if check.input_type == InputType.TEXT:
            # Текст проверяется полностью, как ссылка: история события на всех языках,
            # сверка утверждений, индекс (раньше — только почти дословные совпадения).
            seed = await text_seed(session, check.input_raw, embedder)
            arg = f"doc:{seed.id}"
        return await run_check(
            session,
            arg,
            embedder,
            user_id=check.user_id,
            input_type=InputType(check.input_type),
            cache_hours=settings.check_cache_hours if use_cache else 0,
            llm=llm,
            existing=check,
            ocr_images=ocr_images,
            ocr_rare_langs=ocr_rare_langs,
        )
    except (UnsafeUrl, httpx.HTTPError, ValueError) as exc:
        await session.rollback()
        check = await session.get(Check, check_id)
        check.status = CheckStatus.FAILED
        check.summary = json.dumps({"error": _failure_reason(exc)}, ensure_ascii=False)
        check.finished_at = datetime.now(UTC)
        await session.commit()
        return check


def failure_text(check: Check) -> str:
    try:
        reason = json.loads(check.summary or "{}").get("error")
    except ValueError:
        reason = None
    return texts.FAILED.format(reason=reason or "внутренняя ошибка.")
