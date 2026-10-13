"""Вход по почте одноразовой ссылкой: без паролей, которые можно украсть или подобрать.

Ссылка живёт 30 минут и срабатывает один раз; в базе — только SHA-256 токена. Переход по
ссылке показывает кнопку «Войти» (POST), а не входит сразу: почтовые сканеры сами открывают
ссылки из писем и иначе «съедали» бы их. Наличие адреса в базе наружу не раскрывается.
"""

import hashlib
import re
import secrets
from datetime import UTC, datetime, timedelta
from html import escape

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.models import LoginToken, User
from app.web.mail import send_mail

TOKEN_MINUTES = 30
PER_EMAIL = (3, timedelta(minutes=15))  # не больше 3 писем на адрес за 15 минут
PER_IP = (10, timedelta(hours=1))  # и 10 писем с одного IP за час
_EMAIL = re.compile(r"^[^@\s<>\"']{1,64}@[^@\s<>\"']+\.[a-zа-яё0-9-]{2,}$", re.IGNORECASE)


class LoginError(Exception):
    pass


def normalize_email(raw: str) -> str | None:
    email = raw.strip().lower()
    return email if len(email) <= 254 and _EMAIL.match(email) else None


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


async def _recent(session: AsyncSession, column, value, window: timedelta) -> int:
    since = datetime.now(UTC) - window
    return await session.scalar(
        select(func.count())
        .select_from(LoginToken)
        .where(column == value, LoginToken.created_at >= since)
    )


async def request_login(
    session: AsyncSession, raw_email: str, ip: str | None, link_user: User | None = None
) -> bool:
    """Отправить ссылку входа. False — почта не настроена (ссылка в логе приложения)."""
    email = normalize_email(raw_email)
    if email is None:
        raise LoginError("Похоже, в адресе опечатка. Проверьте и попробуйте ещё раз.")
    limit, window = PER_EMAIL
    if await _recent(session, LoginToken.email, email, window) >= limit:
        raise LoginError("Письмо на этот адрес уже отправлено. Проверьте почту и папку «Спам».")
    limit, window = PER_IP
    if ip and await _recent(session, LoginToken.ip, ip, window) >= limit:
        raise LoginError("Слишком много запросов. Попробуйте через час.")

    token = secrets.token_urlsafe(32)
    session.add(
        LoginToken(
            email=email,
            token_hash=_hash(token),
            ip=ip,
            expires_at=datetime.now(UTC) + timedelta(minutes=TOKEN_MINUTES),
            purpose="link" if link_user else "login",
            user_id=link_user.id if link_user else None,
        )
    )
    await session.commit()

    link = f"{get_settings().public_base_url.rstrip('/')}/auth/email/{token}"
    if link_user is not None:
        # Нейтральная формулировка «подтвердите адрес»: письмо «привяжите адрес к учётной
        # записи» Яндекс отклонил как подозрительное на спам (554 5.7.1, 13.10.2026), а
        # письмо входа того же вида пропустил.
        text = (
            "Здравствуйте!\n\n"
            "Чтобы подтвердить этот адрес в «Сверке», откройте ссылку "
            f"(действует {TOKEN_MINUTES} минут):\n{link}\n\n"
            "Если вы не запрашивали подтверждение, просто удалите это письмо.\n\n"
            "— «Сверка», проект компании «Ангрист»"
        )
        html = (
            "<p>Здравствуйте!</p>"
            f'<p>Чтобы подтвердить этот адрес в «Сверке», нажмите: <a href="{escape(link)}">'
            f"<b>Подтвердить</b></a><br>Ссылка действует {TOKEN_MINUTES} минут.</p>"
            "<p>Если вы не запрашивали подтверждение, просто удалите это письмо.</p>"
            '<p style="color:#6b7385">— «Сверка», проект компании «Ангрист»</p>'
        )
        return await send_mail(email, "Подтверждение адреса в «Сверке»", text, html)
    text = (
        "Здравствуйте!\n\n"
        f"Чтобы войти в «Сверку», откройте ссылку (действует {TOKEN_MINUTES} минут):\n{link}\n\n"
        "Если вы не запрашивали вход, просто удалите это письмо.\n\n"
        "— «Сверка», проект компании «Ангрист»"
    )
    html = (
        "<p>Здравствуйте!</p>"
        f'<p>Чтобы войти в «Сверку», нажмите: <a href="{escape(link)}"><b>Войти</b></a><br>'
        f"Ссылка действует {TOKEN_MINUTES} минут и срабатывает один раз.</p>"
        "<p>Если вы не запрашивали вход, просто удалите это письмо.</p>"
        '<p style="color:#6b7385">— «Сверка», проект компании «Ангрист»</p>'
    )
    return await send_mail(email, "Вход в «Сверку»", text, html)


async def valid_token(session: AsyncSession, token: str) -> LoginToken:
    item = await session.scalar(select(LoginToken).where(LoginToken.token_hash == _hash(token)))
    if item is None or item.used_at is not None:
        raise LoginError("Ссылка уже использована или неверна. Запросите новую на странице входа.")
    if item.expires_at < datetime.now(UTC):
        raise LoginError("Срок ссылки истёк. Запросите новую на странице входа.")
    return item


async def login_by_token(session: AsyncSession, token: str, current: User | None) -> User:
    """Подтвердить ссылку и войти. Возвращает пользователя, которого надо поставить в cookie."""
    from app.web.session import is_anonymous, merge_anonymous

    item = await valid_token(session, token)
    if item.purpose == "link":
        raise LoginError("Это ссылка для привязки почты — откройте её из письма.")
    item.used_at = datetime.now(UTC)
    now = datetime.now(UTC)
    owner = await session.scalar(select(User).where(User.email == item.email))
    if owner is None and current is not None and is_anonymous(current):
        # Анонимному посетителю почта становится учётной записью (история браузера — с ней).
        # Вошедшего через Telegram не трогаем: привязка — только явно, на странице учётной записи.
        owner = current
        owner.email = item.email
    elif owner is None:
        owner = User(email=item.email)
        session.add(owner)
        await session.flush()
    owner.email_verified_at = owner.email_verified_at or now
    owner.pd_consent_at = owner.pd_consent_at or now  # согласие отмечено в форме входа
    if current is not None and current.id != owner.id and is_anonymous(current):
        await merge_anonymous(session, current, owner)
    await session.commit()
    return owner


async def confirm_link(session: AsyncSession, token: str, current: User | None) -> User:
    """Подтвердить привязку почты к учётной записи, которая её запросила (тот же браузер)."""
    from app.web.accounts import AccountError, link_email

    item = await valid_token(session, token)
    if item.purpose != "link":
        raise LoginError("Это ссылка для входа, а не для привязки.")
    if current is None or current.id != item.user_id:
        raise LoginError(
            "Откройте ссылку в том же браузере, где запрашивали привязку, и войдите в ту же "
            "учётную запись."
        )
    item.used_at = datetime.now(UTC)
    try:
        return await link_email(session, current, item.email)
    except AccountError as exc:
        await session.rollback()
        raise LoginError(str(exc)) from exc
