"""Одна учётная запись — почта и Telegram вместе (страница «Учётная запись»).

Привязка — только явным действием вошедшего пользователя, не побочным эффектом входа:
иначе на общем компьютере вход по своей почте после чужого входа через Telegram незаметно
слил бы две чужие учётные записи. Если у почты или Telegram уже есть своя учётная запись,
она вливается в текущую: проверки, жалобы, членство в организации; сама удаляется.
"""

from datetime import UTC, datetime

from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Check, Feedback, LoginToken, OrgInvite, OrgMember, PlanRequest, User


class AccountError(Exception):
    pass


async def merge_accounts(session: AsyncSession, survivor: User, other: User) -> None:
    """Влить other в survivor. Вызывающий код делает commit."""
    if survivor.id == other.id:
        return
    mine = await session.scalar(select(OrgMember).where(OrgMember.user_id == survivor.id))
    theirs = await session.scalar(select(OrgMember).where(OrgMember.user_id == other.id))
    if mine and theirs and mine.org_id != theirs.org_id:
        raise AccountError(
            "Эти учётные записи состоят в разных организациях, а пользователь может быть только "
            "в одной. Попросите владельца одного из кабинетов удалить вас, затем повторите."
        )
    if theirs and mine:
        await session.delete(theirs)  # та же организация — достаточно одного членства
    elif theirs:
        theirs.user_id = survivor.id

    for model, column in (
        (Check, Check.user_id),
        (Feedback, Feedback.user_id),
        (OrgInvite, OrgInvite.used_by),
        (LoginToken, LoginToken.user_id),
        (PlanRequest, PlanRequest.user_id),
    ):
        await session.execute(update(model).where(column == other.id).values({column: survivor.id}))

    email, tg_id = other.email, other.tg_id
    verified = other.email_verified_at
    # Лучшее из двух: личная поправка и действующий платный тариф.
    if other.daily_quota is not None:
        survivor.daily_quota = max(survivor.daily_quota or 0, other.daily_quota)
    if other.plan != "standard" and (survivor.plan == "standard" or not survivor.plan):
        survivor.plan, survivor.plan_until = other.plan, other.plan_until
    await session.execute(delete(User).where(User.id == other.id))
    await session.flush()  # освободить уникальные email/tg_id до переноса
    if survivor.email is None and email:
        survivor.email, survivor.email_verified_at = email, verified
    if survivor.tg_id is None and tg_id:
        survivor.tg_id = tg_id


async def link_email(session: AsyncSession, user: User, email: str) -> User:
    if user.email == email:
        return user
    if user.email is not None:
        raise AccountError("К учётной записи уже привязана другая почта.")
    owner = await session.scalar(select(User).where(User.email == email))
    if owner is not None:
        await merge_accounts(session, user, owner)
    else:
        user.email = email
        user.email_verified_at = datetime.now(UTC)
    await session.commit()
    return user


async def link_telegram(session: AsyncSession, user: User, tg_id: int) -> User:
    if user.tg_id == tg_id:
        return user
    if user.tg_id is not None:
        raise AccountError("К учётной записи уже привязан другой Telegram.")
    owner = await session.scalar(select(User).where(User.tg_id == tg_id))
    if owner is not None:
        await merge_accounts(session, user, owner)
    else:
        user.tg_id = tg_id
    await session.commit()
    return user
