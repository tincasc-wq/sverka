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
    invite = request.query_params.get("invite")
    return templates.TemplateResponse(
        request,
        "admin_orgs.html",
        {
            "nav": "orgs",
            "rows": rows,
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


@protected.post("/orgs/{org_id}")
async def update_org(
    org_id: int,
    session: Session,
    seats: Annotated[int, Form()],
    monthly_quota: Annotated[int, Form()],
    ru_only: Annotated[bool, Form()] = False,
    active: Annotated[bool, Form()] = False,
):
    org = await session.get(Organization, org_id)
    if org is None:
        raise HTTPException(404)
    org.seats = max(1, seats)
    org.monthly_quota = max(0, monthly_quota)
    org.ru_only = ru_only
    org.active = active
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
