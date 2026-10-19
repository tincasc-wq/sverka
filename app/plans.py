"""Тарифы пользователей (ФТ-9.1): какой действует и сколько проверок осталось в его периоде.

Описание тарифов — config/plans.yaml. У пользователя хранится назначенный платный тариф
(users.plan, users.plan_until — ставит админ) и необязательная личная поправка лимита
(users.daily_quota). Лимит общий для бота и сайта.
"""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from functools import lru_cache
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import User

CONFIG_PATH = Path(__file__).resolve().parents[1] / "config" / "plans.yaml"
GUEST, STANDARD = "guest", "standard"
MSK = ZoneInfo("Europe/Moscow")


class Option(BaseModel):
    code: str
    name: str
    # ₽ за каждую проверку месячного объёма; None — доплата по запросу (цену называем в ответе
    # на заявку, в калькуляторе не считаем: российский контур, 16.10.2026, до замера стоимости).
    per_check: float | None = 0
    description: str = ""


class Constructor(BaseModel):
    """Конструктор «Редакции»: цена складывается из мест, объёма проверок и опций."""

    base_price: int
    base_seats: int
    base_checks: int
    max_seats: int
    seat_price: int
    checks_step: int
    checks_step_price: int
    max_checks: int
    options: list[Option] = []


class Plan(BaseModel):
    code: str = ""
    name: str
    daily_checks: int | None = None
    monthly_checks: int | None = None
    seats: int | None = None
    price: str = ""
    paid: bool = False
    org: bool = False
    public: bool = True
    features: list[str] = []
    soon: list[str] = []
    pdf: bool = False  # выгрузка отчёта в PDF
    priority: bool = False  # проверки встают в начало очереди
    schedule: bool = False  # повторная проверка по расписанию (app/watches.py)
    ocr: bool = False  # проверка скриншотов: текст с картинки (app/ocr.py)
    topics: int = 0  # сколько тем мониторинга у организации (app/topics.py)
    constructor: Constructor | None = None

    @property
    def period(self) -> str:
        return "month" if self.monthly_checks is not None else "day"


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


def personal_limit(user: User | None) -> tuple[int, str]:
    """(лимит, период: day | month). Личная поправка админа — в периоде тарифа."""
    plan = plan_for(user)
    if user is not None and user.daily_quota is not None:
        return user.daily_quota, plan.period
    return (
        (plan.monthly_checks or 0, "month")
        if plan.period == "month"
        else (
            plan.daily_checks or 0,
            "day",
        )
    )


def daily_limit(user: User | None) -> int:
    """Лимит в периоде тарифа (имя сохранено ради совместимости вызовов)."""
    return personal_limit(user)[0]


def period_start(period: str) -> datetime:
    now = datetime.now(MSK)
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return start.replace(day=1) if period == "month" else start


async def checks_in_period(session: AsyncSession, user: User, period: str) -> int:
    from sqlalchemy import func, select

    from app.models import Check

    return await session.scalar(
        select(func.count())
        .select_from(Check)
        .where(
            Check.user_id == user.id,
            Check.created_at >= period_start(period),
            Check.channel.is_distinct_from("auto"),  # отслеживание поправок — за счёт сервиса
        )
    )


def quote(plan: Plan, seats: int, checks: int, options: set[str]) -> tuple[int, int, int]:
    """Цена конструктора за месяц и нормализованные места/проверки (сервер не верит браузеру)."""
    c = plan.constructor
    seats = max(c.base_seats, min(seats, c.max_seats))
    checks = max(c.base_checks, min(checks, c.max_checks))
    steps = -(-(checks - c.base_checks) // c.checks_step)  # вверх до целого шага
    checks = c.base_checks + steps * c.checks_step
    price = c.base_price + (seats - c.base_seats) * c.seat_price + steps * c.checks_step_price
    price += sum(
        round(o.per_check * checks) for o in c.options if o.code in options and o.per_check
    )
    return price, seats, checks


def _entitled(user: User | None, membership, feature: str) -> bool:
    """Возможность тарифа: у сотрудников сервиса всегда, у участника организации — по её тарифу."""
    from app.web.auth import is_staff

    if is_staff(user):
        return True
    if membership is not None:
        org_plan = get_plans().get(membership.org.plan)
        return bool(org_plan and getattr(org_plan, feature))
    return getattr(plan_for(user), feature)


def can_export_pdf(user: User | None, membership=None) -> bool:
    return _entitled(user, membership, "pdf")


def can_schedule(user: User | None, membership=None) -> bool:
    return _entitled(user, membership, "schedule")


def can_ocr(user: User | None, membership=None) -> bool:
    return _entitled(user, membership, "ocr")


def has_priority(user: User | None, membership=None) -> bool:
    return _entitled(user, membership, "priority")


# arq берёт задачи по возрастанию времени запуска: задача «из прошлого» обгоняет всех,
# кто встал в очередь позже этого момента. Час — с запасом больше любой реальной очереди.
PRIORITY_HEAD_START = timedelta(hours=1)


def queue_options(priority: bool) -> dict:
    """Аргументы enqueue_job: платные проверки — в начало очереди, без отдельного воркера."""
    return {"_defer_until": datetime.now(UTC) - PRIORITY_HEAD_START} if priority else {}


@dataclass
class Usage:
    plan: Plan
    used: int
    limit: int | None  # None — лимит организации, а не личный
    period: str = "day"
    org_name: str | None = None
    org_used: int | None = None
    org_limit: int | None = None

    @property
    def left(self) -> int | None:
        return None if self.limit is None else max(0, self.limit - self.used)

    @property
    def period_text(self) -> str:
        return "в этом месяце" if self.period == "month" else "сегодня"


async def usage_of(session: AsyncSession, user: User | None) -> Usage:
    from app.orgs.service import checks_this_month, membership_of

    membership = await membership_of(session, user)
    plans = get_plans()
    if membership is not None:
        org = membership.org
        return Usage(
            plan=plans.get(org.plan) or plans["editorial"],
            used=await checks_in_period(session, user, "month"),
            limit=None,
            period="month",
            org_name=org.name,
            org_used=await checks_this_month(session, org),
            org_limit=org.monthly_quota,
        )
    limit, period = personal_limit(user)
    used = await checks_in_period(session, user, period) if user is not None else 0
    return Usage(plan=plan_for(user), used=used, limit=limit, period=period)
