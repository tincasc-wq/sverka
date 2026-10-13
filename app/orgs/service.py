"""Организации (ФТ-9.1, 9.2, 9.6): членство, месячная квота, приглашения, расходы.

Сотрудник организации проверяет за счёт её месячной квоты, а не личного дневного лимита;
отчёты организации закрытые; у организации с ru_only все шаги LLM идут по российским
маршрутам (раздел 12.5).
"""

import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Check, LlmCall, Organization, OrgInvite, OrgMember, User
from app.models.enums import OrgRole

MSK = ZoneInfo("Europe/Moscow")
INVITE_DAYS = 7
USD_RUB = 90


@dataclass
class Membership:
    org: Organization
    member: OrgMember

    @property
    def is_owner(self) -> bool:
        return self.member.role == OrgRole.OWNER


async def membership_of(session: AsyncSession, user: User | None) -> Membership | None:
    if user is None:
        return None
    row = (
        await session.execute(
            select(Organization, OrgMember)
            .join(OrgMember, OrgMember.org_id == Organization.id)
            .where(OrgMember.user_id == user.id)
        )
    ).first()
    return Membership(*row) if row else None


def month_start(now: datetime | None = None) -> datetime:
    now = (now or datetime.now(UTC)).astimezone(MSK)
    return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


async def checks_this_month(session: AsyncSession, org: Organization) -> int:
    return await session.scalar(
        select(func.count())
        .select_from(Check)
        .where(Check.org_id == org.id, Check.created_at >= month_start())
    )


async def quota_refusal(session: AsyncSession, membership: Membership) -> str | None:
    """Почему организации сейчас нельзя проверять; None — можно."""
    org = membership.org
    if not org.active:
        return f"Доступ организации «{org.name}» приостановлен. Обратитесь к владельцу кабинета."
    if await checks_this_month(session, org) >= org.monthly_quota:
        return f"Месячная квота организации «{org.name}» ({org.monthly_quota} проверок) исчерпана."
    return None


async def seats_used(session: AsyncSession, org: Organization) -> int:
    return await session.scalar(
        select(func.count()).select_from(OrgMember).where(OrgMember.org_id == org.id)
    )


async def create_invite(
    session: AsyncSession, org: Organization, role: OrgRole = OrgRole.MEMBER
) -> OrgInvite:
    invite = OrgInvite(
        org_id=org.id,
        token=secrets.token_urlsafe(16),
        role=role,
        expires_at=datetime.now(UTC) + timedelta(days=INVITE_DAYS),
    )
    session.add(invite)
    await session.commit()
    return invite


class InviteError(Exception):
    pass


async def invite_by_token(session: AsyncSession, token: str) -> tuple[OrgInvite, Organization]:
    invite = await session.scalar(select(OrgInvite).where(OrgInvite.token == token))
    if invite is None or invite.used_at is not None:
        raise InviteError("Приглашение не найдено или уже использовано.")
    if invite.expires_at < datetime.now(UTC):
        raise InviteError("Срок приглашения истёк. Попросите владельца кабинета прислать новое.")
    return invite, await session.get(Organization, invite.org_id)


async def accept_invite(session: AsyncSession, token: str, user: User) -> Organization:
    invite, org = await invite_by_token(session, token)
    current = await membership_of(session, user)
    if current is not None:
        if current.org.id == org.id:
            return org
        raise InviteError(
            f"Вы уже состоите в организации «{current.org.name}». "
            "Один пользователь — одна организация."
        )
    if await seats_used(session, org) >= org.seats:
        raise InviteError(f"В организации «{org.name}» заняты все места ({org.seats}).")
    session.add(OrgMember(org_id=org.id, user_id=user.id, role=invite.role))
    invite.used_by = user.id
    invite.used_at = datetime.now(UTC)
    await session.commit()
    return org


async def remove_member(session: AsyncSession, org: Organization, member_id: int) -> bool:
    member = await session.get(OrgMember, member_id)
    if member is None or member.org_id != org.id or member.role == OrgRole.OWNER:
        return False  # владельца не удаляем — иначе кабинет останется без управления
    await session.delete(member)
    await session.commit()
    return True


async def members_with_usage(session: AsyncSession, org: Organization) -> list[dict]:
    used = (
        select(Check.user_id, func.count().label("n"))
        .where(Check.org_id == org.id, Check.created_at >= month_start())
        .group_by(Check.user_id)
        .subquery()
    )
    rows = await session.execute(
        select(OrgMember, User, used.c.n)
        .join(User, User.id == OrgMember.user_id)
        .outerjoin(used, used.c.user_id == OrgMember.user_id)
        .where(OrgMember.org_id == org.id)
        .order_by(OrgMember.role.desc(), OrgMember.created_at)
    )
    return [
        {"member": m, "user": u, "checks": n or 0, "label": member_label(m, u)} for m, u, n in rows
    ]


def member_label(member: OrgMember, user: User) -> str:
    """Как показать сотрудника: имени у нас нет (храним минимум, 152-ФЗ)."""
    kind = "владелец" if member.role == OrgRole.OWNER else "сотрудник"
    via = "Telegram" if user.tg_id else "браузер"
    return f"{kind} №{member.id} · вход: {via}"


async def org_checks(session: AsyncSession, org: Organization, limit: int = 100) -> list[Check]:
    return list(
        await session.scalars(
            select(Check).where(Check.org_id == org.id).order_by(Check.id.desc()).limit(limit)
        )
    )


async def month_cost_rub(session: AsyncSession, org: Organization) -> float:
    """Себестоимость LLM за месяц (ФТ-9.6) — для владельца и для расчёта маржи."""
    usd = await session.scalar(
        select(func.coalesce(func.sum(LlmCall.cost_usd), 0))
        .join(Check, Check.id == LlmCall.check_id)
        .where(Check.org_id == org.id, LlmCall.created_at >= month_start())
    )
    return round(float(usd) * USD_RUB, 2)


async def can_view_check(session: AsyncSession, check: Check, user: User | None) -> bool:
    """Отчёт организации — только её сотрудникам (конфиденциальность запросов, 12.5)."""
    if check.org_id is None:
        return True
    membership = await membership_of(session, user)
    return membership is not None and membership.org.id == check.org_id
