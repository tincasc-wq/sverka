"""Кабинет организации (ФТ-9.2): квота, сотрудники, приглашения, общая история, слежения,
расходы, выгрузка истории в CSV и месячный PDF-отчёт."""

from typing import Annotated

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.db import get_session
from app.orgs.history import CATEGORY_RU, HistoryFilter, csv_bytes, history, recent_months
from app.orgs.service import (
    InviteError,
    Membership,
    accept_invite,
    checks_this_month,
    create_invite,
    invite_by_token,
    members_with_usage,
    membership_of,
    month_cost_rub,
    remove_member,
    seats_used,
)
from app.web.session import current_user, ensure_user, set_user_cookie
from app.web.site import page

router = APIRouter()
Session = Annotated[AsyncSession, Depends(get_session)]


def invite_url(token: str) -> str:
    return f"{get_settings().public_base_url.rstrip('/')}/join/{token}"


@router.get("/org", response_class=HTMLResponse)
async def cabinet(request: Request, session: Session):
    user = await current_user(request, session)
    membership = await membership_of(session, user)
    if membership is None:
        return await page(request, session, "org_none.html", user=user)
    org = membership.org
    members = await members_with_usage(session, org)
    authors = {m["user"].id: m["label"] for m in members}
    filters = HistoryFilter.from_params(request.query_params)
    new_invite = request.query_params.get("invite")
    from datetime import UTC, datetime, timedelta

    from app.plans import get_plans

    plans = get_plans()
    now = datetime.now(UTC)
    paid_state = None
    if org.paid_until is not None:
        paid_state = (
            "expired"
            if org.paid_until < now
            else "soon"
            if org.paid_until - now < timedelta(days=7)
            else "ok"
        )
    return await page(
        request,
        session,
        "org.html",
        user=user,
        org=org,
        is_owner=membership.is_owner,
        used=await checks_this_month(session, org),
        seats=await seats_used(session, org),
        members=members,
        checks=await history(session, org, filters),
        filters=filters,
        categories=CATEGORY_RU,
        months=recent_months(),
        authors=authors,
        watches=await _watches(session, org, user, authors),
        cost=await month_cost_rub(session, org) if membership.is_owner else None,
        invite=invite_url(new_invite) if new_invite and membership.is_owner else None,
        removed=request.query_params.get("removed"),
        plan=plans.get(org.plan),
        org_plans={code: p for code, p in plans.items() if p.org and p.public},
        paid_state=paid_state,
        requested=request.query_params.get("requested"),
    )


@router.post("/org/change-plan")
async def change_plan(
    request: Request,
    session: Session,
    plan: Annotated[str, Form()],
    seats: Annotated[int, Form()] = 0,
    checks: Annotated[int, Form()] = 0,
    ru_only: Annotated[str, Form()] = "",
    comment: Annotated[str, Form()] = "",
):
    """Владелец просит изменить тариф: заявка в базу и сообщение администратору в Telegram."""
    from html import escape

    from app.models import PlanRequest
    from app.plans import get_plans, quote
    from app.web.notify import notify_admin

    membership = await _owner_membership(request, session)
    if membership is None:
        return RedirectResponse("/org", status_code=303)
    org, user = membership.org, await current_user(request, session)
    plans = get_plans()
    target = plans.get(plan)
    if target is None or not target.org:
        return RedirectResponse("/org", status_code=303)
    want_ru = ru_only == "on"
    parts = [f"{seats or org.seats} мест", f"{checks or org.monthly_quota} проверок в месяц"]
    if want_ru:
        parts.append("российский контур")
    price = None
    if target.constructor:
        price, q_seats, q_checks = quote(target, seats, checks, {"ru_only"} if want_ru else set())
        parts[:2] = [f"{q_seats} мест", f"{q_checks} проверок в месяц"]
    wish = ", ".join(parts) + (f" — {price:,} ₽/мес".replace(",", " ") if price else "")
    current = f"сейчас «{org.plan}»: {org.seats} мест, {org.monthly_quota} проверок"
    contact = (user.email if user and user.email else "") or (
        f"Telegram ID {user.tg_id}" if user and user.tg_id else "владелец кабинета"
    )
    session.add(
        PlanRequest(
            user_id=user.id if user else None,
            plan=plan,
            contact=contact[:255],
            org_name=org.name[:255],
            comment="\n".join(
                x
                for x in (f"Смена тарифа организации: {wish} ({current})", comment.strip()[:2000])
                if x
            ),
            ip=request.client.host if request.client else None,
        )
    )
    await session.commit()
    lines = [
        f"🦊 <b>Организация «{escape(org.name)}» просит изменить тариф</b>",
        f"Хочет: «{escape(target.name)}» — {escape(wish)}",
        f"Сейчас: {escape(current)}",
        f"Контакт: {escape(contact)}",
    ]
    if comment.strip():
        lines.append(f"Комментарий: {escape(comment.strip()[:2000])}")
    lines.append(f"Изменить: {get_settings().public_base_url.rstrip('/')}/admin/orgs")
    await notify_admin("\n".join(lines))
    return RedirectResponse("/org?requested=1#plan", status_code=303)


async def _owner_membership(request: Request, session: AsyncSession) -> Membership | None:
    membership = await membership_of(session, await current_user(request, session))
    return membership if membership and membership.is_owner else None


@router.post("/org/invite")
async def new_invite(request: Request, session: Session):
    membership = await _owner_membership(request, session)
    if membership is None:
        return RedirectResponse("/org", status_code=303)
    if await seats_used(session, membership.org) >= membership.org.seats:
        return RedirectResponse("/org#invite", status_code=303)
    invite = await create_invite(session, membership.org)
    return RedirectResponse(f"/org?invite={invite.token}#invite", status_code=303)


@router.post("/org/members/{member_id}/remove")
async def remove(request: Request, member_id: int, session: Session):
    membership = await _owner_membership(request, session)
    if membership is None:
        return RedirectResponse("/org", status_code=303)
    done = await remove_member(session, membership.org, member_id)
    return RedirectResponse(f"/org?removed={'1' if done else '0'}", status_code=303)


@router.get("/join/{token}", response_class=HTMLResponse)
async def join_page(request: Request, token: str, session: Session):
    try:
        invite, org = await invite_by_token(session, token)
        error = None
    except InviteError as exc:
        invite = org = None
        error = str(exc)
    return await page(request, session, "org_join.html", org=org, invite=invite, error=error)


@router.post("/join/{token}")
async def join(request: Request, token: str, session: Session):
    user, new_user = await ensure_user(request, session)
    try:
        await accept_invite(session, token, user)
        response = RedirectResponse("/org", status_code=303)
    except InviteError as exc:
        await session.commit()
        response = await page(
            request, session, "org_join.html", user=user, org=None, invite=None, error=str(exc)
        )
        response.status_code = 409
    if new_user:
        set_user_cookie(response, user.id)
    return response


async def _watches(session: AsyncSession, org, user, authors: dict[int, str]) -> list[dict]:
    from app.models import Check
    from app.watches import org_watches, subscribed_ids

    watches = await org_watches(session, org.id)
    mine = await subscribed_ids(session, user, watches)
    items = []
    for w in watches:
        last = await session.get(Check, w.last_check_id) if w.last_check_id else None
        items.append(
            {
                "watch": w,
                "title": (w.last_state or {}).get("title") or "новость",
                "author": authors.get(w.user_id, "бывший сотрудник"),
                "own": w.user_id == user.id,
                "subscribed": w.id in mine,
                "token": last.public_token if last else None,
            }
        )
    return items


@router.post("/watches/{watch_id}/subscribe")
async def watch_subscribe(request: Request, watch_id: int, session: Session):
    from app.watches import subscribe

    user = await current_user(request, session)
    if user is not None:
        await subscribe(session, user, watch_id, on=True)
    return RedirectResponse("/org#watches", status_code=303)


@router.post("/watches/{watch_id}/unsubscribe")
async def watch_unsubscribe(request: Request, watch_id: int, session: Session):
    from app.watches import subscribe

    user = await current_user(request, session)
    if user is not None:
        await subscribe(session, user, watch_id, on=False)
    return RedirectResponse("/org#watches", status_code=303)


@router.get("/org/export.csv")
async def export_csv(request: Request, session: Session):
    """Выгрузка истории (с теми же фильтрами, что на странице) — для Excel."""
    from app.orgs.history import EXPORT_LIMIT

    membership = await membership_of(session, await current_user(request, session))
    if membership is None:
        return RedirectResponse("/org", status_code=303)
    org = membership.org
    authors = {m["user"].id: m["label"] for m in await members_with_usage(session, org)}
    rows = await history(
        session, org, HistoryFilter.from_params(request.query_params), limit=EXPORT_LIMIT
    )
    body = csv_bytes(rows, authors, get_settings().public_base_url.rstrip("/"))
    return Response(
        body,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="sverka-history.csv"'},
    )


@router.get("/org/report.pdf")
async def month_report(request: Request, session: Session):
    """Месячный отчёт организации: итоги, сотрудники, категории, спорные новости."""
    from datetime import datetime

    from fastapi.concurrency import run_in_threadpool

    from app.orgs.history import month_summary
    from app.orgs.service import MSK
    from app.web.report import msk, render_pdf, templates

    membership = await membership_of(session, await current_user(request, session))
    if membership is None:
        return RedirectResponse("/org", status_code=303)
    org = membership.org
    month = HistoryFilter.from_params(request.query_params).month or recent_months(1)[0]
    authors = {m["user"].id: m["label"] for m in await members_with_usage(session, org)}
    summary = await month_summary(session, org, month, authors)
    base = get_settings().public_base_url.rstrip("/")
    html = templates.get_template("org_report_pdf.html").render(
        org=org,
        s=summary,
        categories=CATEGORY_RU,
        month_ru=_month_ru(month),
        site=base.removeprefix("https://").removeprefix("http://"),
        base=base,
        now=msk(datetime.now(MSK)),
    )
    pdf = await run_in_threadpool(render_pdf, html)
    return Response(
        pdf,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="sverka-{month}.pdf"'},
    )


MONTHS_RU = (
    "январь",
    "февраль",
    "март",
    "апрель",
    "май",
    "июнь",
    "июль",
    "август",
    "сентябрь",
    "октябрь",
    "ноябрь",
    "декабрь",
)


def _month_ru(month: str) -> str:
    year, num = month.split("-")
    return f"{MONTHS_RU[int(num) - 1]} {year}"
