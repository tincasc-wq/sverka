"""Оповещения владельцу организации: израсходовано 80% и 100% месячной квоты.

Раз в час воркер смотрит, сколько проверок организация сделала в этом месяце. На каждый
порог — одно сообщение в месяц (метка в Redis), в Telegram владельца или на почту.
"""

import logging
from html import escape

from sqlalchemy import select

from app.core.config import get_settings
from app.models import Organization, OrgMember, User
from app.models.enums import OrgRole
from app.orgs.service import checks_this_month, month_start

log = logging.getLogger("sverka.orgs")

THRESHOLDS = (100, 80)  # от старшего к младшему: перешли 100% — про 80% уже не пишем
KEEP_SECONDS = 40 * 86400


def threshold_reached(used: int, quota: int) -> int | None:
    if quota <= 0:
        return None
    return next((t for t in THRESHOLDS if used * 100 >= quota * t), None)


def alert_text(org: Organization, used: int, threshold: int):
    from app.watches import Notice

    counts = f"{used} из {org.monthly_quota}"
    if threshold >= 100:
        head = f"Организация «{org.name}» израсходовала месячную квоту: {counts}."
        tail = "Новые проверки до начала месяца не пройдут. Чтобы продолжить, расширьте тариф."
    else:
        head = f"Организация «{org.name}» израсходовала {threshold}% квоты: {counts}."
        tail = "Если проверок не хватит до конца месяца, расширьте тариф в кабинете."
    url = f"{get_settings().public_base_url.rstrip('/')}/org#plan"
    return Notice(
        html=f"⚠️ <b>{escape(head)}</b>\n{escape(tail)}\n{escape(url)}",
        text=f"{head}\n{tail}\n{url}",
        subject=f"Сверка: квота «{org.name}» — {threshold}%",
    )


async def run_quota_alerts(session_factory, redis, bot=None) -> int:
    """Возвращает число отправленных оповещений."""
    from app.watches import notify

    month = month_start().strftime("%Y-%m")
    sent = 0
    async with session_factory() as session:
        orgs = list(
            await session.scalars(select(Organization).where(Organization.active.is_(True)))
        )
        for org in orgs:
            used = await checks_this_month(session, org)
            threshold = threshold_reached(used, org.monthly_quota)
            if threshold is None:
                continue
            key = f"sverka:quota-alert:{org.id}:{month}:{threshold}"
            if redis is not None and not await redis.set(key, 1, ex=KEEP_SECONDS, nx=True):
                continue  # уже сообщали в этом месяце
            if threshold >= 100 and redis is not None:
                await redis.set(f"sverka:quota-alert:{org.id}:{month}:80", 1, ex=KEEP_SECONDS)
            owners = await session.scalars(
                select(User)
                .join(OrgMember, OrgMember.user_id == User.id)
                .where(OrgMember.org_id == org.id, OrgMember.role == OrgRole.OWNER)
            )
            notice = alert_text(org, used, threshold)
            for owner in owners:
                if await notify(owner, notice, bot):
                    sent += 1
            log.info("квота %s: %d%% (%d/%d)", org.name, threshold, used, org.monthly_quota)
    return sent
