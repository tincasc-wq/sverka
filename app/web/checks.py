"""Проверки, запущенные с сайта: запись создаётся сразу (страница ожидания), работа — в воркере."""

import asyncio
import json
import secrets
from datetime import UTC, datetime

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot import texts
from app.bot.inputs import extract_input
from app.bot.service import quota_exceeded, similar_to_text
from app.check import run_check
from app.collectors.http import UnsafeUrl
from app.core.config import get_settings
from app.embeddings import Embedder
from app.llm import LlmClient
from app.models import Check, User
from app.models.enums import CheckStatus, InputType

NOT_UNDERSTOOD = (
    "Не понял, что проверять. Вставьте ссылку на публикацию или текст новости "
    "(от нескольких предложений)."
)


class CheckRefused(Exception):
    """Проверку не запускаем: непонятный ввод или исчерпан лимит."""


async def create_web_check(session: AsyncSession, user: User, value: str) -> Check:
    user_input = extract_input(value)
    if user_input is None:
        raise CheckRefused(NOT_UNDERSTOOD)
    if await quota_exceeded(session, user):
        raise CheckRefused(
            f"Лимит {user.daily_quota} проверок в сутки исчерпан. Попробуйте завтра"
            + (" или войдите через Telegram — лимит станет больше." if user.tg_id is None else ".")
        )
    check = Check(
        user_id=user.id,
        input_type=user_input.type,
        input_raw=user_input.value,
        status=CheckStatus.QUEUED,
        public_token=secrets.token_urlsafe(12),
    )
    session.add(check)
    await session.commit()
    return check


def _failure_reason(exc: Exception) -> str:
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
    check.status = CheckStatus.RUNNING
    await session.commit()
    try:
        if check.input_type == InputType.TEXT:
            text = check.input_raw[: settings.embedding_max_chars]
            [vector] = await asyncio.to_thread(embedder.embed, [text])
            matches = await similar_to_text(session, vector, settings.text_search_days)
            check.summary = json.dumps(
                {"matches": matches, "days": settings.text_search_days}, ensure_ascii=False
            )
            check.status = CheckStatus.DONE
            check.finished_at = datetime.now(UTC)
            await session.commit()
            return check
        return await run_check(
            session,
            check.input_raw,
            embedder,
            user_id=check.user_id,
            input_type=InputType(check.input_type),
            cache_hours=settings.check_cache_hours if use_cache else 0,
            llm=llm,
            existing=check,
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
