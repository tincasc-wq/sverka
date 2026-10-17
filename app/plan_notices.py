"""Уведомление о смене тарифа: сообщение в Telegram (или письмо) и плашка на сайте.

Тариф пока меняет администратор вручную, и человек не знал, что ему включили Pro
(отзыв тестового журналиста 16.10.2026). Плашка висит, пока её не закроют.
"""

import logging
from datetime import datetime
from html import escape

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Organization, OrgMember, User
from app.models.enums import OrgRole

log = logging.getLogger("sverka.plans")


def _date(value: datetime | None) -> str:
    from app.web.report import msk_date

    return msk_date(value) if value else ""


def _personal_lines(user: User) -> list[str]:
    from app.plans import personal_limit, plan_for

    plan = plan_for(user)
    limit, period = personal_limit(user)
    lines = [f"{limit} проверок {'в месяц' if period == 'month' else 'в сутки'}"]
    if plan.pdf:
        lines.append("PDF-отчёты")
    if plan.schedule:
        lines.append("повторная проверка по расписанию")
    if plan.priority:
        lines.append("приоритет в очереди")
    return lines


def personal_notice_text(user: User, old_plan_name: str) -> str:
    """Короткий текст для плашки на сайте."""
    from app.plans import plan_for

    plan = plan_for(user)
    until = f" до {_date(user.plan_until)}" if user.plan_until else ""
    was = f" (был «{old_plan_name}»)" if old_plan_name and old_plan_name != plan.name else ""
    return f"Ваш тариф — «{plan.name}»{until}{was}. Теперь: {', '.join(_personal_lines(user))}."


def org_notice_text(org: Organization) -> str:
    from app.plans import get_plans

    plan = get_plans().get(org.plan)
    name = plan.name if plan else org.plan
    parts = [f"{org.seats} мест", f"{org.monthly_quota} проверок в месяц"]
    if org.ru_only:
        parts.append("российский контур")
    text = f"Тариф организации «{org.name}» — «{name}»: {', '.join(parts)}"
    if org.paid_until:
        text += f", оплачено до {_date(org.paid_until)}"
    text += "."
    if not org.active:
        text += " Доступ организации приостановлен."
    return text


async def _announce(user: User, text: str) -> bool:
    from app.core.config import get_settings
    from app.watches import Notice, notify
    from app.web.notify import BotApi

    user.plan_notice = text
    url = f"{get_settings().public_base_url.rstrip('/')}/pricing"
    notice = Notice(
        html=f"🦊 <b>Тариф изменён</b>\n{escape(text)}\n{escape(url)}",
        text=f"{text}\n\n{url}",
        subject="Сверка: тариф изменён",
    )
    try:
        return await notify(user, notice, BotApi())
    except Exception:  # noqa: BLE001 — уведомление не должно мешать смене тарифа
        log.warning("уведомление о тарифе пользователю %s не отправлено", user.id, exc_info=True)
        return False


async def announce_personal(user: User, old_plan_name: str) -> bool:
    return await _announce(user, personal_notice_text(user, old_plan_name))


async def announce_org(session: AsyncSession, org: Organization) -> int:
    """Владельцам организации. Возвращает, скольким доставлено."""
    owners = await session.scalars(
        select(User)
        .join(OrgMember, OrgMember.user_id == User.id)
        .where(OrgMember.org_id == org.id, OrgMember.role == OrgRole.OWNER)
    )
    text = org_notice_text(org)
    sent = 0
    for owner in owners:
        sent += bool(await _announce(owner, text))
    return sent
