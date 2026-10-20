"""Пользователи бота, лимиты (ФТ-1.4) и сама проверка для чата — общий код бота и воркера."""

import json
from dataclasses import dataclass
from datetime import datetime, timedelta

import httpx
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot import texts
from app.check import run_check, text_seed
from app.collectors.http import UnsafeUrl
from app.collectors.maxru import MaxStub
from app.collectors.store import DocumentIn
from app.core.config import get_settings
from app.embeddings import Embedder
from app.llm import LlmClient
from app.models import Check, Document, Source, User
from app.models.enums import InputType
from app.pipeline.graph import USER_TEXT_PREFIX
from app.plans import checks_in_period, personal_limit, plan_for
from app.registry.service import source_card


async def get_or_create_user(session: AsyncSession, tg_id: int) -> User:
    await session.execute(
        insert(User).values(tg_id=tg_id).on_conflict_do_nothing(index_elements=["tg_id"])
    )
    return await session.scalar(select(User).where(User.tg_id == tg_id))


async def checks_today(session: AsyncSession, user: User) -> int:
    start = datetime.now(texts.MSK).replace(hour=0, minute=0, second=0, microsecond=0)
    return await session.scalar(
        select(func.count())
        .select_from(Check)
        .where(Check.user_id == user.id, Check.created_at >= start)
    )


async def quota_exceeded(session: AsyncSession, user: User) -> bool:
    limit, period = personal_limit(user)
    return await checks_in_period(session, user, period) >= limit


async def check_refusal(session: AsyncSession, user: User) -> str | None:
    """Почему пользователю сейчас нельзя проверять; None — можно.

    Сотрудник организации проверяет за счёт её месячной квоты (ФТ-9.1), остальные —
    в пределах личного дневного лимита.
    """
    from app.orgs.service import membership_of, quota_refusal

    membership = await membership_of(session, user)
    if membership is not None:
        return await quota_refusal(session, membership)
    if await quota_exceeded(session, user):
        limit, period = personal_limit(user)
        return texts.QUOTA.format(
            limit=limit,
            plan=plan_for(user).name,
            period="в месяц" if period == "month" else "в сутки",
            when="в следующем месяце" if period == "month" else "завтра",
        )
    return None


def report_url(check: Check) -> str:
    return f"{get_settings().public_base_url.rstrip('/')}/r/{check.public_token}"


@dataclass
class ChatReply:
    text: str
    check_id: int | None = None  # для кнопок «проверить снова» и «сообщить об ошибке»


async def similar_to_text(
    session: AsyncSession, vector: list[float], days: int, limit: int = 8
) -> list[dict]:
    """Сценарий 3: публикации, близкие к тексту, — самые ранние сверху."""
    since = datetime.now(texts.MSK) - timedelta(days=days)
    distance = Document.embedding.cosine_distance(vector)
    rows = await session.execute(
        select(Document, Source)
        .outerjoin(Source, Source.id == Document.source_id)
        .where(Document.embedding.is_not(None), Document.first_seen_at >= since, distance <= 0.2)
        .order_by(distance)
        .limit(limit)
    )
    matches = [
        {
            "time": (doc.published_at or doc.first_seen_at).isoformat(),
            "source": source.name if source else doc.url.split("/")[2],
            "url": doc.url,
            "title": " ".join((doc.title or doc.text or doc.url).split())[:90],
        }
        for doc, source in rows
    ]
    return sorted(matches, key=lambda m: m["time"])


async def check_for_chat(
    session: AsyncSession,
    value: str,
    input_type: InputType,
    tg_id: int,
    embedder: Embedder,
    *,
    use_cache: bool = True,
    fallback_text: str | None = None,
    fallback_date: str | None = None,
    llm: LlmClient | None = None,
) -> ChatReply:
    from app.orgs.service import membership_of
    from app.plans import can_ocr

    settings = get_settings()
    user = await get_or_create_user(session, tg_id)
    membership = await membership_of(session, user)
    org_id = membership.org.id if membership else None
    if llm is not None and membership is not None:
        llm = llm.restricted(membership.org.ru_only)

    arg = value
    if input_type == InputType.TEXT:
        # Текст — полная проверка, как у ссылки (история, сверка, индекс).
        seed = await text_seed(session, value, embedder)
        arg = f"doc:{seed.id}"

    try:
        check = await run_check(
            session,
            arg,
            embedder,
            user_id=user.id,
            input_type=input_type,
            cache_hours=settings.check_cache_hours if use_cache else 0,
            llm=llm,
            org_id=org_id,
            channel="bot",
            input_raw=value,
            ocr_images=user.ocr_images and can_ocr(user, membership),
            ocr_rare_langs=user.ocr_rare_langs and can_ocr(user, membership),
            ocr_allowed=can_ocr(user, membership),
            fallback=DocumentIn(
                url=value,
                text=fallback_text,
                published_at=datetime.fromisoformat(fallback_date) if fallback_date else None,
            )
            if fallback_text
            else None,
        )
    except UnsafeUrl:
        await session.rollback()
        return ChatReply(texts.FAILED.format(reason="ссылка ведёт на недоступный адрес."))
    except MaxStub:
        from app.web.checks import MAX_STUB_REASON

        await session.rollback()
        return ChatReply(
            texts.FAILED.format(reason=MAX_STUB_REASON[0].lower() + MAX_STUB_REASON[1:])
        )
    except (httpx.HTTPError, ValueError) as exc:
        await session.rollback()
        from app.check import BotProtected

        reason = "страница не открылась или на ней нет текста."
        if isinstance(exc, BotProtected):
            from app.web.checks import BOT_WALL_REASON

            reason = BOT_WALL_REASON.replace("в поле проверки", "сюда сообщением")
        elif isinstance(exc, httpx.HTTPStatusError):
            reason = f"сайт ответил ошибкой {exc.response.status_code}."
        return ChatReply(texts.FAILED.format(reason=reason))

    report = json.loads(check.summary)
    seed = await session.get(Document, check.document_id)
    card = None
    if seed and seed.source_id:
        card = await source_card(session, await session.get(Source, seed.source_id))
    handle = None
    if seed and not card and not seed.url.startswith(USER_TEXT_PREFIX):
        parts = seed.url.split("/")
        handle = "@" + parts[3] if parts[2] == "t.me" else parts[2]
    return ChatReply(texts.format_check(report, card, report_url(check), handle), check.id)
