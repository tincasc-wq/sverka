"""Посетитель сайта: подписанная cookie с id пользователя и вход через Telegram.

Без входа посетитель — анонимный пользователь, привязанный к браузеру: история проверок
живёт, пока жива cookie. Вход через Telegram привязывает браузер к пользователю бота
(та же история и тот же лимит) и переносит на него анонимные проверки.
"""

import hashlib
import hmac
import logging
import secrets
import time

from fastapi import Request, Response
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.models import Check, User

log = logging.getLogger(__name__)

COOKIE = "sverka_user"
MAX_AGE = 365 * 24 * 3600
_fallback_key = secrets.token_hex(32)


def _key() -> bytes:
    return (get_settings().secret_key or _fallback_key).encode()


def sign(user_id: int) -> str:
    mac = hmac.new(_key(), str(user_id).encode(), hashlib.sha256).hexdigest()[:32]
    return f"{user_id}.{mac}"


def unsign(value: str | None) -> int | None:
    if not value or "." not in value:
        return None
    raw, mac = value.split(".", 1)
    if not raw.isdigit():
        return None
    expected = hmac.new(_key(), raw.encode(), hashlib.sha256).hexdigest()[:32]
    return int(raw) if hmac.compare_digest(mac, expected) else None


def set_user_cookie(response: Response, user_id: int) -> None:
    response.set_cookie(
        COOKIE,
        sign(user_id),
        max_age=MAX_AGE,
        httponly=True,
        samesite="lax",  # формы с чужих сайтов cookie не получат — защита от CSRF
        secure=get_settings().public_base_url.startswith("https://"),
    )


async def current_user(request: Request, session: AsyncSession) -> User | None:
    user_id = unsign(request.cookies.get(COOKIE))
    return await session.get(User, user_id) if user_id else None


async def ensure_user(request: Request, session: AsyncSession) -> tuple[User, bool]:
    """Пользователь из cookie или новый анонимный. Второе значение — нужно ли ставить cookie."""
    user = await current_user(request, session)
    if user is not None:
        return user, False
    user = User()
    session.add(user)
    await session.flush()
    return user, True


def telegram_login_valid(data: dict[str, str], bot_token: str, max_age_s: int = 86400) -> bool:
    """Проверка подписи виджета «Войти через Telegram» (core.telegram.org/widgets/login)."""
    received = data.get("hash")
    if not received or not bot_token or not data.get("auth_date", "").isdigit():
        return False
    fields = "\n".join(f"{k}={v}" for k, v in sorted(data.items()) if k != "hash")
    secret = hashlib.sha256(bot_token.encode()).digest()
    expected = hmac.new(secret, fields.encode(), hashlib.sha256).hexdigest()
    fresh = time.time() - int(data["auth_date"]) < max_age_s
    return fresh and hmac.compare_digest(expected, received)


def is_anonymous(user: User) -> bool:
    """Пользователь браузера без входа: ни Telegram, ни подтверждённой почты."""
    return user.tg_id is None and user.email is None


async def merge_anonymous(session: AsyncSession, anonymous: User, user: User) -> None:
    """Перенести на вошедшего пользователя то, что накопилось в браузере до входа.

    Проверки — всегда; членство в организации — если у пользователя её ещё нет
    (иначе вступивший по приглашению до входа терял бы доступ к кабинету).
    """
    from app.models import OrgMember

    await session.execute(
        update(Check).where(Check.user_id == anonymous.id).values(user_id=user.id)
    )
    has_org = await session.scalar(select(OrgMember.id).where(OrgMember.user_id == user.id))
    if not has_org:
        await session.execute(
            update(OrgMember).where(OrgMember.user_id == anonymous.id).values(user_id=user.id)
        )


async def login_telegram(session: AsyncSession, anonymous: User | None, tg_id: int) -> User:
    """Пользователь бота с этим tg_id (или новый); накопленное в браузере — на него."""
    from app.bot.service import get_or_create_user

    user = await get_or_create_user(session, tg_id)
    if anonymous is not None and anonymous.id != user.id and is_anonymous(anonymous):
        await merge_anonymous(session, anonymous, user)
    await session.commit()
    return user
