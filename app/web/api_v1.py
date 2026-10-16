"""API для организаций (v1): отправить новость на проверку, забрать результат, узнать остаток квоты.

Доступ — по ключу организации из кабинета: заголовок «Authorization: Bearer sv_…» или
«X-API-Key: sv_…». Проверки идут в месячную квоту организации, как с сайта.
"""

import json
import logging
from datetime import UTC, datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, Field
from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.db import get_session
from app.models import Check
from app.models.enums import CheckStatus
from app.orgs.apikeys import ApiCaller, authenticate

log = logging.getLogger("sverka.api")

router = APIRouter(prefix="/api/v1", tags=["Проверки"])
Session = Annotated[AsyncSession, Depends(get_session)]

RATE_PER_MINUTE = {"write": 30, "read": 300}
_redis: Redis | None = None


async def _rate_limited(key_id: int, kind: str) -> bool:
    """Не больше N запросов в минуту на ключ. Если Redis недоступен — не мешаем работать."""
    global _redis
    try:
        if _redis is None:
            _redis = Redis.from_url(get_settings().redis_url)
        bucket = f"sverka:api-rate:{key_id}:{kind}:{datetime.now(UTC).strftime('%Y%m%d%H%M')}"
        count = await _redis.incr(bucket)
        if count == 1:
            await _redis.expire(bucket, 120)
        return count > RATE_PER_MINUTE[kind]
    except Exception:  # noqa: BLE001
        log.warning("api rate limit unavailable", exc_info=True)
        return False


async def _caller(
    session: AsyncSession, authorization: str | None, x_api_key: str | None
) -> ApiCaller:
    raw = x_api_key
    if raw is None and authorization and authorization.lower().startswith("bearer "):
        raw = authorization[7:].strip()
    caller = await authenticate(session, raw)
    if caller is None:
        raise HTTPException(
            401, "Нужен действующий ключ API организации", {"WWW-Authenticate": "Bearer"}
        )
    return caller


def caller_dep(kind: str):
    async def dependency(
        session: Session,
        authorization: Annotated[str | None, Header()] = None,
        x_api_key: Annotated[str | None, Header()] = None,
    ) -> ApiCaller:
        caller = await _caller(session, authorization, x_api_key)
        if await _rate_limited(caller.key.id, kind):
            raise HTTPException(
                429, "Слишком много запросов: подождите минуту", {"Retry-After": "60"}
            )
        return caller

    return dependency


Writer = Annotated[ApiCaller, Depends(caller_dep("write"))]
Reader = Annotated[ApiCaller, Depends(caller_dep("read"))]


class CheckIn(BaseModel):
    input: str = Field(
        min_length=5,
        max_length=20000,
        description="Ссылка на публикацию (сайт, t.me, max.ru) или текст новости.",
        examples=["https://tass.ru/obschestvo/12345678"],
    )


class Primary(BaseModel):
    source: str | None = None
    url: str | None = None
    time: str | None = None


class Result(BaseModel):
    title: str | None = Field(None, description="Заголовок проверенной публикации")
    url: str | None = None
    index: float | None = Field(None, description="Индекс подтверждённости, 0–100")
    status: str | None = Field(None, description="Код итога, например widely_confirmed")
    status_text: str | None = Field(None, description="Итог по-русски")
    publications: int | None = Field(None, description="Публикаций о событии")
    independent: int | None = Field(None, description="Независимых групп источников")
    primary: Primary | None = Field(None, description="Первоисточник")
    corrections: list[dict] = Field(default_factory=list, description="Поправки и опровержения")
    matches: list[dict] | None = Field(None, description="Для текста без события: совпадения")


class CheckOut(BaseModel):
    id: str = Field(description="Идентификатор проверки (он же токен отчёта)")
    status: Literal["queued", "running", "done", "failed"]
    created_at: datetime
    finished_at: datetime | None = None
    report_url: str = Field(description="Отчёт на сайте — виден сотрудникам организации")
    error: str | None = None
    result: Result | None = None


class UsageOut(BaseModel):
    organization: str
    plan: str
    month_used: int
    month_quota: int
    month_left: int
    active: bool


def _result(summary: dict) -> Result:
    if "matches" in summary:
        return Result(matches=summary["matches"])
    analysis = summary.get("analysis") or {}
    seed = summary.get("seed") or {}
    primary = summary.get("primary")
    return Result(
        title=seed.get("title"),
        url=None if str(seed.get("url", "")).startswith("user-text://") else seed.get("url"),
        index=analysis.get("index"),
        status=analysis.get("status"),
        status_text=analysis.get("status_text"),
        publications=summary.get("core_publications", summary.get("publications")),
        independent=summary.get("independent_core", summary.get("independent_reports")),
        primary=Primary(**{k: primary.get(k) for k in ("source", "url", "time")})
        if primary
        else None,
        corrections=[
            {k: c.get(k) for k in ("source", "time", "quote", "discrepancy")}
            for c in analysis.get("corrections") or []
        ],
    )


def _out(check: Check) -> CheckOut:
    base = get_settings().public_base_url.rstrip("/")
    try:
        summary = json.loads(check.summary or "{}")
    except ValueError:
        summary = {}
    done = check.status == CheckStatus.DONE
    return CheckOut(
        id=check.public_token,
        status=check.status,
        created_at=check.created_at,
        finished_at=check.finished_at,
        report_url=f"{base}/r/{check.public_token}",
        error=summary.get("error") if check.status == CheckStatus.FAILED else None,
        result=_result(summary) if done and summary else None,
    )


@router.post("/checks", status_code=202, response_model=CheckOut)
async def create_check(body: CheckIn, caller: Writer, session: Session):
    """Поставить новость на проверку. Ответ сразу; результат — через GET /api/v1/checks/{id}.

    Проверка обычно занимает 30–90 секунд. Опрашивайте не чаще раза в 5 секунд.
    """
    from app.plans import has_priority, queue_options
    from app.web.checks import CheckRefused, create_web_check
    from app.web.queue import enqueue

    try:
        check = await create_web_check(session, caller.user, body.input, channel="api")
    except CheckRefused as exc:
        quota = "квота" in str(exc) or "приостановлен" in str(exc)
        raise HTTPException(402 if quota else 422, str(exc)) from exc
    priority = has_priority(caller.user, caller.membership)
    await enqueue("web_check", check.id, True, **queue_options(priority))
    return _out(check)


@router.get("/checks/{check_id}", response_model=CheckOut)
async def get_check(check_id: str, caller: Reader, session: Session):
    """Состояние и результат проверки. Доступны все проверки организации, не только по API."""
    check = await session.scalar(
        select(Check).where(
            Check.public_token == check_id, Check.org_id == caller.membership.org.id
        )
    )
    if check is None:
        raise HTTPException(404, "Проверки нет")
    return _out(check)


@router.get("/usage", response_model=UsageOut, tags=["Организация"])
async def usage(caller: Reader, session: Session):
    """Сколько проверок израсходовано в текущем месяце."""
    from app.orgs.service import checks_this_month

    org = caller.membership.org
    used = await checks_this_month(session, org)
    return UsageOut(
        organization=org.name,
        plan=org.plan,
        month_used=used,
        month_quota=org.monthly_quota,
        month_left=max(0, org.monthly_quota - used),
        active=org.active,
    )
