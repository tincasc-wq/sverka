"""Тарифы пользователей (ФТ-9.1): какой действует и сколько проверок осталось на сегодня.

Описание тарифов — config/plans.yaml. У пользователя хранится назначенный платный тариф
(users.plan, users.plan_until — ставит админ) и необязательная личная поправка лимита
(users.daily_quota). Лимит общий для бота и сайта.
"""

from dataclasses import dataclass
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path

import yaml
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import User

CONFIG_PATH = Path(__file__).resolve().parents[1] / "config" / "plans.yaml"
GUEST, STANDARD = "guest", "standard"


class Plan(BaseModel):
    code: str = ""
    name: str
    daily_checks: int | None = None
    price: str = ""
    paid: bool = False
    org: bool = False
    public: bool = True
    features: list[str] = []
    soon: list[str] = []


@lru_cache
def get_plans(path: Path = CONFIG_PATH) -> dict[str, Plan]:
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return {code: Plan(code=code, **item) for code, item in (data.get("plans") or {}).items()}


def is_guest(user: User | None) -> bool:
    return user is None or (user.tg_id is None and user.email is None)


def plan_for(user: User | None, now: datetime | None = None) -> Plan:
    """Личный тариф пользователя (организация проверяется отдельно — у неё своя квота)."""
    plans = get_plans()
    if is_guest(user):
        return plans[GUEST]
    assigned = plans.get(user.plan or "")
    still_valid = user.plan_until is None or user.plan_until > (now or datetime.now(UTC))
    if assigned and assigned.paid and still_valid:
        return assigned
    return plans[STANDARD]


def daily_limit(user: User | None) -> int:
    if user is not None and user.daily_quota is not None:
        return user.daily_quota  # личная поправка админа
    return plan_for(user).daily_checks or 0


@dataclass
class Usage:
    plan: Plan
    used: int
    limit: int | None  # None — лимит организации, а не личный
    org_name: str | None = None
    org_used: int | None = None
    org_limit: int | None = None

    @property
    def left(self) -> int | None:
        return None if self.limit is None else max(0, self.limit - self.used)


async def usage_of(session: AsyncSession, user: User | None) -> Usage:
    from app.bot.service import checks_today
    from app.orgs.service import checks_this_month, membership_of

    used = await checks_today(session, user) if user is not None else 0
    membership = await membership_of(session, user)
    if membership is not None:
        org = membership.org
        return Usage(
            plan=get_plans()["editorial"],
            used=used,
            limit=None,
            org_name=org.name,
            org_used=await checks_this_month(session, org),
            org_limit=org.monthly_quota,
        )
    return Usage(plan=plan_for(user), used=used, limit=daily_limit(user))
