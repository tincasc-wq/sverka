"""Кабинет организации (ФТ-9.2): квота, сотрудники, приглашения, общая история, расходы."""

from typing import Annotated

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.db import get_session
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
    org_checks,
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
    new_invite = request.query_params.get("invite")
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
        checks=await org_checks(session, org),
        authors={m["user"].id: m["label"] for m in members},
        cost=await month_cost_rub(session, org) if membership.is_owner else None,
        invite=invite_url(new_invite) if new_invite and membership.is_owner else None,
        removed=request.query_params.get("removed"),
    )


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
