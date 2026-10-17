"""Админка аналитика (М8, этап 5): реестр источников, разметка исходов, жалобы."""

import json
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.db import get_session
from app.models import (
    Check,
    Document,
    Feedback,
    Organization,
    Outcome,
    Source,
    Story,
    StoryDocument,
)
from app.models.enums import DecidedBy, FeedbackStatus, OrgRole, Stance, StoryStatus, Verdict
from app.registry.service import last_scores, set_outcome, source_card
from app.web.auth import COOKIE, require_admin, token_valid
from app.web.report import msk

templates = Jinja2Templates(directory=Path(__file__).parent / "templates")
templates.env.filters["msk"] = msk
router = APIRouter(prefix="/admin")
protected = APIRouter(prefix="/admin", dependencies=[Depends(require_admin)])
Session = Annotated[AsyncSession, Depends(get_session)]

STANCE_LABELS = {
    "supports": "распространяла",
    "contradicts": "опровергала",
    "unrelated": "не по теме",
}
VERDICT_LABELS = {
    "confirmed": "Подтвердилась",
    "refuted": "Опровергнута",
    "mixed": "Смешанно",
    "unknown": "Неизвестно",
}


@router.get("/login", response_class=HTMLResponse)
async def login_form(request: Request):
    return templates.TemplateResponse(request, "login.html", {"error": None})


@router.post("/login")
async def login(request: Request, token: Annotated[str, Form()]):
    if not token_valid(token):
        return templates.TemplateResponse(
            request, "login.html", {"error": "Неверный токен"}, status_code=401
        )
    response = RedirectResponse("/admin/stories", status_code=303)
    response.set_cookie(COOKIE, token, httponly=True, samesite="strict")
    return response


@protected.get("/", response_class=HTMLResponse)
async def index():
    return RedirectResponse("/admin/stories", status_code=303)


@protected.get("/sources", response_class=HTMLResponse)
async def sources(request: Request, session: Session):
    rows = list(await session.scalars(select(Source).order_by(Source.type, Source.name)))
    scores = await last_scores(session)
    counts = dict(
        (
            await session.execute(
                select(Document.source_id, func.count()).group_by(Document.source_id)
            )
        ).all()
    )
    return templates.TemplateResponse(
        request, "sources.html", {"sources": rows, "scores": scores, "counts": counts}
    )


@protected.get("/sources/{source_id}", response_class=HTMLResponse)
async def source_page(request: Request, source_id: int, session: Session):
    source = await session.get(Source, source_id)
    if source is None:
        raise HTTPException(404)
    feedback = list(
        await session.scalars(
            select(Feedback).where(Feedback.source_id == source_id).order_by(Feedback.id.desc())
        )
    )
    recent = list(
        await session.scalars(
            select(Document)
            .where(Document.source_id == source_id)
            .order_by(Document.first_seen_at.desc())
            .limit(15)
        )
    )
    return templates.TemplateResponse(
        request,
        "source.html",
        {
            "source": source,
            "card": await source_card(session, source),
            "feedback": feedback,
            "recent": recent,
        },
    )


@protected.get("/stories", response_class=HTMLResponse)
async def stories(request: Request, session: Session, status: str = "open"):
    size = (
        select(StoryDocument.story_id, func.count().label("n"))
        .group_by(StoryDocument.story_id)
        .subquery()
    )
    rows = await session.execute(
        select(Story, size.c.n)
        .join(size, size.c.story_id == Story.id)
        .where(Story.status == status)
        .order_by(Story.first_seen_at.desc())
        .limit(100)
    )
    return templates.TemplateResponse(
        request, "stories.html", {"stories": rows.all(), "status": status}
    )


@protected.get("/stories/{story_id}", response_class=HTMLResponse)
async def story_page(request: Request, story_id: int, session: Session):
    story = await session.get(Story, story_id)
    if story is None:
        raise HTTPException(404)
    rows = await session.execute(
        select(StoryDocument, Document, Source)
        .join(Document, Document.id == StoryDocument.document_id)
        .outerjoin(Source, Source.id == Document.source_id)
        .where(StoryDocument.story_id == story_id)
        .order_by(func.coalesce(Document.published_at, Document.first_seen_at))
    )
    outcomes = list(
        await session.scalars(
            select(Outcome).where(Outcome.story_id == story_id).order_by(Outcome.decided_at.desc())
        )
    )
    check = await session.scalar(
        select(Check).where(Check.story_id == story_id).order_by(Check.id.desc())
    )
    report = json.loads(check.summary) if check and check.summary else None
    return templates.TemplateResponse(
        request,
        "story.html",
        {
            "story": story,
            "rows": rows.all(),
            "outcomes": outcomes,
            "report": report,
            "stance_labels": STANCE_LABELS,
            "verdict_labels": VERDICT_LABELS,
        },
    )


@protected.post("/stories/{story_id}/outcome")
async def mark_outcome(request: Request, story_id: int, session: Session):
    form = await request.form()
    verdict = Verdict(form["verdict"])
    stances = {}
    for key, value in form.items():
        if key.startswith("stance_") and value:
            stances[int(key.removeprefix("stance_"))] = Stance(value)
    await set_outcome(
        session,
        story_id,
        verdict,
        DecidedBy.ANALYST,
        evidence_url=(form.get("evidence_url") or None),
        stances=stances,
    )
    await session.commit()
    # Сразу к следующей неразмеченной истории — разметка за минуту (приёмка этапа 5).
    next_id = await session.scalar(
        select(Story.id)
        .where(Story.status == StoryStatus.OPEN)
        .order_by(Story.first_seen_at.desc())
        .limit(1)
    )
    return RedirectResponse(
        f"/admin/stories/{next_id}" if next_id else "/admin/stories", status_code=303
    )


@protected.get("/feedback", response_class=HTMLResponse)
async def feedback_list(request: Request, session: Session, status: str = "open"):
    rows = await session.execute(
        select(Feedback, Source)
        .outerjoin(Source, Source.id == Feedback.source_id)
        .where(Feedback.status == status)
        .order_by(Feedback.id.desc())
        .limit(200)
    )
    return templates.TemplateResponse(
        request, "feedback.html", {"rows": rows.all(), "status": status}
    )


@protected.post("/feedback/{feedback_id}")
async def resolve_feedback(
    feedback_id: int,
    session: Session,
    status: Annotated[FeedbackStatus, Form()],
    resolution: Annotated[str, Form()] = "",
):
    item = await session.get(Feedback, feedback_id)
    if item is None:
        raise HTTPException(404)
    item.status = status
    item.resolution = resolution or None
    await session.commit()
    return RedirectResponse("/admin/feedback", status_code=303)


@protected.post("/sources/{source_id}/tracked")
async def toggle_tracked(source_id: int, session: Session):
    source = await session.get(Source, source_id)
    if source is None:
        raise HTTPException(404)
    source.is_tracked = not source.is_tracked
    await session.commit()
    return RedirectResponse(f"/admin/sources/{source_id}", status_code=303)


# ---- организации (ФТ-9.2, 9.5): создаёт аналитик по договору, оплата — по счёту вручную ----


@protected.get("/orgs", response_class=HTMLResponse)
async def orgs(request: Request, session: Session):
    from app.orgs.service import checks_this_month, month_cost_rub, seats_used

    rows = []
    for org in await session.scalars(select(Organization).order_by(Organization.id.desc())):
        rows.append(
            {
                "org": org,
                "seats": await seats_used(session, org),
                "used": await checks_this_month(session, org),
                "cost": await month_cost_rub(session, org),
            }
        )
    from app.plans import get_plans

    invite = request.query_params.get("invite")
    return templates.TemplateResponse(
        request,
        "admin_orgs.html",
        {
            "nav": "orgs",
            "rows": rows,
            "org_plans": {code: p for code, p in get_plans().items() if p.org},
            "msk_tz": __import__("zoneinfo").ZoneInfo("Europe/Moscow"),
            "invite": f"{get_settings().public_base_url.rstrip('/')}/join/{invite}"
            if invite
            else None,
        },
    )


@protected.post("/orgs")
async def create_org(
    session: Session,
    name: Annotated[str, Form()],
    plan: Annotated[str, Form()] = "editorial",
    seats: Annotated[int, Form()] = 5,
    monthly_quota: Annotated[int, Form()] = 3000,
    ru_only: Annotated[bool, Form()] = False,
):
    from app.orgs.service import create_invite

    org = Organization(
        name=name.strip()[:255],
        plan=plan.strip()[:32] or "editorial",
        seats=max(1, seats),
        monthly_quota=max(0, monthly_quota),
        ru_only=ru_only,
    )
    session.add(org)
    await session.commit()
    invite = await create_invite(session, org, OrgRole.OWNER)
    return RedirectResponse(f"/admin/orgs?invite={invite.token}", status_code=303)


def _end_of_day(value: str):
    """«2026-11-30» → конец этого дня по Москве; пусто — срок не задан."""
    from datetime import datetime, time
    from zoneinfo import ZoneInfo

    value = value.strip()
    if not value:
        return None
    day = datetime.fromisoformat(value).date()
    return datetime.combine(day, time(23, 59), tzinfo=ZoneInfo("Europe/Moscow"))


@protected.post("/orgs/{org_id}")
async def update_org(
    org_id: int,
    session: Session,
    seats: Annotated[int, Form()],
    monthly_quota: Annotated[int, Form()],
    ru_only: Annotated[bool, Form()] = False,
    active: Annotated[bool, Form()] = False,
    paid_until: Annotated[str, Form()] = "",
):
    from app.plan_notices import announce_org

    org = await session.get(Organization, org_id)
    if org is None:
        raise HTTPException(404)
    before = (org.seats, org.monthly_quota, org.ru_only, org.active, org.paid_until)
    org.seats = max(1, seats)
    org.monthly_quota = max(0, monthly_quota)
    org.ru_only = ru_only
    org.active = active
    org.paid_until = _end_of_day(paid_until)
    if (org.seats, org.monthly_quota, org.ru_only, org.active, org.paid_until) != before:
        await announce_org(session, org)
    await session.commit()
    return RedirectResponse("/admin/orgs", status_code=303)


@protected.post("/orgs/{org_id}/owner-invite")
async def owner_invite(org_id: int, session: Session):
    from app.orgs.service import create_invite

    org = await session.get(Organization, org_id)
    if org is None:
        raise HTTPException(404)
    invite = await create_invite(session, org, OrgRole.OWNER)
    return RedirectResponse(f"/admin/orgs?invite={invite.token}", status_code=303)


@protected.get("/stats", response_class=HTMLResponse)
async def stats(request: Request, session: Session, days: int = 14):
    from app.web.stats import collect

    data = await collect(session, max(1, min(days, 90)))
    return templates.TemplateResponse(request, "admin_stats.html", {"nav": "stats", **data})


# ---- пользователи и тарифы (ФТ-9.1): назначение вручную до подключения оплаты ----


@protected.get("/users", response_class=HTMLResponse)
async def users(request: Request, session: Session, q: str = ""):
    from app.models import OrgMember, User
    from app.plans import get_plans, personal_limit, plan_for

    query = select(User).order_by(User.id.desc()).limit(100)
    q = q.strip()
    if q:
        if q.isdigit():
            query = query.where((User.id == int(q)) | (User.tg_id == int(q)))
        else:
            like = f"%{q.lower()}%"
            query = query.where(User.email.ilike(like) | User.admin_note.ilike(f"%{q}%"))
    else:
        query = query.where((User.tg_id.is_not(None)) | (User.email.is_not(None)))
    rows = []
    for user in await session.scalars(query):
        total = await session.scalar(
            select(func.count()).select_from(Check).where(Check.user_id == user.id)
        )
        org = await session.scalar(
            select(Organization.name)
            .join(OrgMember, OrgMember.org_id == Organization.id)
            .where(OrgMember.user_id == user.id)
        )
        rows.append(
            {
                "user": user,
                "plan": plan_for(user),
                "limit": personal_limit(user)[0],
                "total": total,
                "org": org,
            }
        )
    plans = [p for p in get_plans().values() if p.paid or p.code == "standard"]
    from app.models import PlanRequest

    requests = list(
        await session.scalars(
            select(PlanRequest).order_by(PlanRequest.status, PlanRequest.id.desc()).limit(30)
        )
    )
    return templates.TemplateResponse(
        request,
        "admin_users.html",
        {
            "nav": "users",
            "rows": rows,
            "q": q,
            "plans": plans,
            "all_plans": get_plans(),
            "requests": requests,
            "saved": request.query_params.get("saved"),
        },
    )


@protected.post("/users/{user_id}/plan")
async def set_user_plan(
    user_id: int,
    session: Session,
    plan: Annotated[str, Form()],
    until: Annotated[str, Form()] = "",
    daily_quota: Annotated[str, Form()] = "",
):
    from datetime import UTC, datetime
    from zoneinfo import ZoneInfo

    from app.models import User
    from app.plan_notices import announce_personal
    from app.plans import get_plans, plan_for

    user = await session.get(User, user_id)
    if user is None or plan not in get_plans():
        raise HTTPException(404)
    before = (plan_for(user).code, user.plan_until, user.daily_quota)
    old_name = plan_for(user).name
    user.plan = plan
    user.plan_until = (
        datetime.strptime(until, "%Y-%m-%d")
        .replace(hour=23, minute=59, tzinfo=ZoneInfo("Europe/Moscow"))
        .astimezone(UTC)
        if until.strip()
        else None
    )
    user.daily_quota = int(daily_quota) if daily_quota.strip().isdigit() else None
    if (plan_for(user).code, user.plan_until, user.daily_quota) != before:
        await announce_personal(user, old_name)
    await session.commit()
    return RedirectResponse(f"/admin/users?q={user_id}&saved=1", status_code=303)


@protected.post("/users/{user_id}/note")
async def set_user_note(user_id: int, session: Session, note: Annotated[str, Form()] = ""):
    from app.models import User

    user = await session.get(User, user_id)
    if user is None:
        raise HTTPException(404)
    user.admin_note = note.strip()[:255] or None
    await session.commit()
    return RedirectResponse(f"/admin/users?q={user_id}&saved=1", status_code=303)


@protected.post("/requests/{request_id}/done")
async def plan_request_done(request_id: int, session: Session):
    from app.models import PlanRequest

    item = await session.get(PlanRequest, request_id)
    if item is None:
        raise HTTPException(404)
    item.status = "done"
    await session.commit()
    return RedirectResponse("/admin/users#requests", status_code=303)


@protected.post("/users/{user_id}/role")
async def set_user_role(
    request: Request, user_id: int, session: Session, role: Annotated[str, Form()]
):
    """Роли меняет только администратор (или вход по токену): аналитик не повысит сам себя."""
    from app.models import User
    from app.models.enums import UserRole
    from app.web.session import current_user

    me = await current_user(request, session)
    if not token_valid(request.cookies.get(COOKIE)) and (me is None or me.role != UserRole.ADMIN):
        raise HTTPException(403, "Роли меняет администратор")
    user = await session.get(User, user_id)
    if user is None or role not in {r.value for r in UserRole}:
        raise HTTPException(404)
    user.role = role
    await session.commit()
    return RedirectResponse(f"/admin/users?q={user_id}&saved=1", status_code=303)
