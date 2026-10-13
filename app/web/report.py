"""Публичный веб-отчёт по проверке: /r/<token> (ФТ-7.2). Ссылку на него присылают бот и сайт."""

import json
from datetime import datetime
from pathlib import Path
from typing import Annotated
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.db import get_session
from app.models import Check, Feedback
from app.models.enums import CheckStatus, FeedbackStatus
from app.orgs.service import can_view_check, membership_of
from app.plans import can_export_pdf
from app.web.session import current_user

router = APIRouter()
Session = Annotated[AsyncSession, Depends(get_session)]
templates = Jinja2Templates(directory=Path(__file__).parent / "templates")
STATIC = Path(__file__).parent / "static"
MSK = ZoneInfo("Europe/Moscow")


def msk(value: str | datetime | None) -> str:
    if value is None:
        return ""
    moment = datetime.fromisoformat(value) if isinstance(value, str) else value
    return moment.astimezone(MSK).strftime("%d.%m.%Y %H:%M")


templates.env.filters["msk"] = msk


async def check_by_token(session: AsyncSession, token: str) -> Check:
    check = await session.scalar(select(Check).where(Check.public_token == token))
    if check is None:
        raise HTTPException(404, "Отчёт не найден")
    return check


@router.get("/r/{token}", response_class=HTMLResponse)
async def report_page(request: Request, token: str, session: Session):
    check = await check_by_token(session, token)
    user = await current_user(request, session)
    if not await can_view_check(session, check, user):
        raise HTTPException(404, "Отчёт не найден")  # чужой отчёт организации — как будто нет
    if check.status != CheckStatus.DONE or not check.summary:
        # Ещё идёт или не удалась — страница ожидания знает, что показать.
        return RedirectResponse(f"/c/{token}", status_code=303)
    report = json.loads(check.summary)
    membership = await membership_of(session, user)
    context = {
        "check": check,
        "report": report,
        "user": user,
        "membership": membership,
        "pdf": can_export_pdf(user, membership),
        "thanks": request.query_params.get("thanks") == "1",
    }
    template = "text_report.html" if "matches" in report else "report.html"
    return templates.TemplateResponse(request, template, context)


def render_pdf(html: str) -> bytes:
    from weasyprint import HTML  # тяжёлый импорт — только когда PDF действительно нужен

    return HTML(string=html, base_url=str(STATIC)).write_pdf()


async def build_pdf(check: Check, membership=None) -> bytes:
    """PDF отчёта — общий для сайта и бота; вёрстка в отдельном потоке, чтобы не держать цикл."""
    base = get_settings().public_base_url.rstrip("/")
    html = templates.get_template("report_pdf.html").render(
        check=check,
        report=json.loads(check.summary),
        site=base.removeprefix("https://").removeprefix("http://"),
        report_url=f"{base}/r/{check.public_token}",
        now=msk(datetime.now(MSK)),
        org_name=membership.org.name if membership else None,
    )
    return await run_in_threadpool(render_pdf, html)


@router.get("/r/{token}/pdf")
async def report_pdf(request: Request, token: str, session: Session):
    """PDF-версия отчёта — для платных тарифов и организаций (раздел 12.2)."""
    check = await check_by_token(session, token)
    user = await current_user(request, session)
    if not await can_view_check(session, check, user):
        raise HTTPException(404, "Отчёт не найден")
    if check.status != CheckStatus.DONE or not check.summary:
        return RedirectResponse(f"/c/{token}", status_code=303)
    membership = await membership_of(session, user)
    if not can_export_pdf(user, membership):
        return RedirectResponse("/pricing?need=pdf", status_code=303)
    pdf = await build_pdf(check, membership)
    return Response(
        pdf,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="sverka-{token[:8]}.pdf"'},
    )


@router.post("/r/{token}/feedback")
async def report_feedback(
    request: Request, token: str, session: Session, text: Annotated[str, Form()]
):
    """«Сообщить об ошибке» на странице отчёта (ФТ-7.4) — попадает в админку к аналитику."""
    check = await check_by_token(session, token)
    user = await current_user(request, session)
    if not await can_view_check(session, check, user):
        raise HTTPException(404, "Отчёт не найден")
    text = text.strip()[:4000]
    if len(text) >= 3:
        session.add(
            Feedback(
                check_id=check.id,
                user_id=user.id if user else None,
                text=text,
                status=FeedbackStatus.OPEN,
            )
        )
        await session.commit()
    return RedirectResponse(f"/r/{token}?thanks=1#feedback", status_code=303)
