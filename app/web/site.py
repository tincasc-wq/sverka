"""Публичный сайт (этап 6): проверка с сайта, история, поиск по архиву, реестр, методика, вход."""

import json
from datetime import UTC, datetime, timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.db import get_session
from app.core.scoring import get_scoring
from app.models import Check, Document, Source, SourceScore
from app.models.enums import CheckStatus
from app.orgs.service import can_view_check, membership_of
from app.registry.legal import display_name, legal_note
from app.registry.service import source_card
from app.web.checks import CheckRefused, create_web_check, failure_text
from app.web.email_auth import LoginError, login_by_token, request_login, valid_token
from app.web.queue import enqueue
from app.web.report import check_by_token, templates
from app.web.session import (
    COOKIE,
    current_user,
    ensure_user,
    login_telegram,
    set_user_cookie,
    telegram_login_valid,
)

router = APIRouter()
Session = Annotated[AsyncSession, Depends(get_session)]

SOURCE_TYPES = {
    "agency": "агентство",
    "official": "официальный",
    "media": "СМИ",
    "tg": "Telegram",
    "vk": "VK",
}


async def page(request: Request, session: AsyncSession, name: str, **context):
    """Страница сайта: пользователь и его организация нужны шапке (ссылка на кабинет)."""
    user = context.pop("user") if "user" in context else await current_user(request, session)
    membership = await membership_of(session, user)
    return templates.TemplateResponse(
        request, name, {"site": True, "user": user, "membership": membership, **context}
    )


@router.get("/", response_class=HTMLResponse)
async def home(request: Request, session: Session):
    user = await current_user(request, session)
    recent = []
    if user is not None:
        recent = list(
            await session.scalars(
                select(Check).where(Check.user_id == user.id).order_by(Check.id.desc()).limit(5)
            )
        )
    return await page(request, session, "home.html", user=user, recent=recent, value="", error=None)


@router.post("/check")
async def start_check(
    request: Request,
    session: Session,
    value: Annotated[str, Form()],
    fresh: Annotated[bool, Form()] = False,
):
    user, new_user = await ensure_user(request, session)
    try:
        check = await create_web_check(session, user, value.strip())
    except CheckRefused as exc:
        await session.commit()
        response = await page(
            request, session, "home.html", user=user, recent=[], value=value, error=str(exc)
        )
        response.status_code = 422
    else:
        await enqueue("web_check", check.id, not fresh)
        response = RedirectResponse(f"/c/{check.public_token}", status_code=303)
    if new_user:
        set_user_cookie(response, user.id)
    return response


@router.get("/c/{token}", response_class=HTMLResponse)
async def wait_page(request: Request, token: str, session: Session):
    check = await check_by_token(session, token)
    if not await can_view_check(session, check, await current_user(request, session)):
        raise HTTPException(404, "Отчёт не найден")
    if check.status == CheckStatus.DONE:
        return RedirectResponse(f"/r/{token}", status_code=303)
    error = failure_text(check) if check.status == CheckStatus.FAILED else None
    return await page(request, session, "wait.html", check=check, error=error)


@router.get("/c/{token}/status")
async def check_status(request: Request, token: str, session: Session) -> JSONResponse:
    check = await check_by_token(session, token)
    if not await can_view_check(session, check, await current_user(request, session)):
        raise HTTPException(404, "Отчёт не найден")
    body = {"status": check.status}
    if check.status == CheckStatus.FAILED:
        body["error"] = failure_text(check)
    return JSONResponse(body, headers={"Cache-Control": "no-store"})


@router.get("/history", response_class=HTMLResponse)
async def history(request: Request, session: Session):
    user = await current_user(request, session)
    checks = []
    if user is not None:
        checks = list(
            await session.scalars(
                select(Check).where(Check.user_id == user.id).order_by(Check.id.desc()).limit(100)
            )
        )
    return await page(request, session, "history.html", user=user, checks=checks)


def _like(q: str) -> str:
    escaped = q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


@router.get("/search", response_class=HTMLResponse)
async def search(request: Request, session: Session, q: str = ""):
    q = q.strip()[:200]
    results = []
    days = get_settings().search_days
    if len(q) >= 3:
        since = datetime.now(UTC) - timedelta(days=days)
        moment = func.coalesce(Document.published_at, Document.first_seen_at)
        pattern = _like(q)
        rows = await session.execute(
            select(Document, Source)
            .outerjoin(Source, Source.id == Document.source_id)
            .where(
                moment >= since,
                or_(Document.title.ilike(pattern), Document.text.ilike(pattern)),
            )
            .order_by(moment.desc())
            .limit(50)
        )
        for doc, source in rows:
            text = " ".join((doc.text or "").split())
            pos = text.lower().find(q.lower())
            start = max(0, pos - 80) if pos >= 0 else 0
            results.append(
                {
                    "time": doc.published_at or doc.first_seen_at,
                    "source": display_name(source.name, legal_note(source.legal_flags))
                    if source
                    else doc.url.split("/")[2],
                    "source_id": source.id if source else None,
                    "url": doc.url,
                    "title": doc.title or text[:120],
                    "snippet": ("…" if start else "") + text[start : start + 240],
                }
            )
    return await page(
        request,
        session,
        "search.html",
        user=await current_user(request, session),
        q=q,
        results=results,
        days=days,
    )


@router.get("/sources", response_class=HTMLResponse)
async def sources(request: Request, session: Session, lang: str = ""):
    latest = (
        select(SourceScore.source_id, func.max(SourceScore.id).label("score_id"))
        .group_by(SourceScore.source_id)
        .subquery()
    )
    docs = (
        select(Document.source_id, func.count().label("n")).group_by(Document.source_id).subquery()
    )
    query = (
        select(Source, SourceScore, docs.c.n)
        .outerjoin(latest, latest.c.source_id == Source.id)
        .outerjoin(SourceScore, SourceScore.id == latest.c.score_id)
        .outerjoin(docs, docs.c.source_id == Source.id)
        .where(Source.is_tracked)
        .order_by(Source.lang.desc(), Source.type, Source.name)
    )
    if lang in ("ru", "en"):
        query = query.where(Source.lang == lang)
    min_outcomes = get_scoring().source_rating.min_outcomes_to_show
    rows = [
        {
            "source": source,
            "name": display_name(source.name, legal_note(source.legal_flags)),
            "type": SOURCE_TYPES.get(source.type, source.type),
            "documents": n or 0,
            "rating": score if score and score.n_observations >= min_outcomes else None,
        }
        for source, score, n in await session.execute(query)
    ]
    return await page(
        request,
        session,
        "public_sources.html",
        user=await current_user(request, session),
        rows=rows,
        lang=lang,
    )


@router.get("/sources/{source_id}", response_class=HTMLResponse)
async def source_page(request: Request, source_id: int, session: Session):
    source = await session.get(Source, source_id)
    if source is None or not source.is_tracked:
        raise HTTPException(404, "Источника нет в реестре")
    card = await source_card(session, source)
    latest = list(
        await session.scalars(
            select(Document)
            .where(Document.source_id == source.id)
            .order_by(func.coalesce(Document.published_at, Document.first_seen_at).desc())
            .limit(15)
        )
    )
    return await page(
        request,
        session,
        "public_source.html",
        user=await current_user(request, session),
        source=source,
        card=card,
        type=SOURCE_TYPES.get(source.type, source.type),
        latest=latest,
    )


@router.get("/methodology", response_class=HTMLResponse)
async def methodology(request: Request, session: Session):
    return await page(
        request,
        session,
        "methodology.html",
        user=await current_user(request, session),
        cfg=get_scoring(),
    )


async def _login_page(request: Request, session: AsyncSession, **context):
    settings = get_settings()
    # Виджет Telegram работает только на домене, заданном у @BotFather (/setdomain).
    widget = bool(settings.telegram_bot_username) and settings.public_base_url.startswith(
        "https://"
    )
    context.setdefault("error", None)
    return await page(
        request,
        session,
        "site_login.html",
        widget=widget,
        bot=settings.telegram_bot_username,
        auth_url=settings.public_base_url.rstrip("/") + "/auth/telegram",
        **context,
    )


@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request, session: Session):
    error = (
        "Не удалось подтвердить вход. Попробуйте ещё раз."
        if request.query_params.get("error")
        else None
    )
    return await _login_page(request, session, error=error)


@router.post("/login/email", response_class=HTMLResponse)
async def login_email(request: Request, session: Session, email: Annotated[str, Form()]):
    ip = request.client.host if request.client else None
    try:
        sent = await request_login(session, email, ip)
    except LoginError as exc:
        response = await _login_page(request, session, email=email, error=str(exc))
        response.status_code = 422
        return response
    return await _login_page(request, session, email_sent=email.strip().lower(), mail_ready=sent)


@router.get("/auth/email/{token}", response_class=HTMLResponse)
async def email_confirm_page(request: Request, token: str, session: Session):
    # Только показываем кнопку: почтовые сканеры открывают ссылки сами (GET), вход — по POST.
    try:
        item = await valid_token(session, token)
        error = None
    except LoginError as exc:
        item, error = None, str(exc)
    return await page(
        request,
        session,
        "email_confirm.html",
        token=token,
        email=item.email if item else None,
        error=error,
    )


@router.post("/auth/email/{token}")
async def email_confirm(request: Request, token: str, session: Session):
    try:
        user = await login_by_token(session, token, await current_user(request, session))
    except LoginError as exc:
        await session.rollback()
        return await page(
            request, session, "email_confirm.html", token=token, email=None, error=str(exc)
        )
    response = RedirectResponse("/history", status_code=303)
    set_user_cookie(response, user.id)
    return response


@router.get("/auth/telegram")
async def auth_telegram(request: Request, session: Session):
    data = dict(request.query_params)
    if (
        not telegram_login_valid(data, get_settings().telegram_bot_token)
        or not data.get("id", "").isdigit()
    ):
        return RedirectResponse("/login?error=1", status_code=303)
    user = await login_telegram(session, await current_user(request, session), int(data["id"]))
    response = RedirectResponse("/history", status_code=303)
    set_user_cookie(response, user.id)
    return response


@router.post("/logout")
async def logout():
    response = RedirectResponse("/", status_code=303)
    response.delete_cookie(COOKIE)
    return response


def summary_of(check: Check) -> dict:
    """Короткая строка для истории: индекс и статус или число совпадений по тексту."""
    try:
        report = json.loads(check.summary or "{}")
    except ValueError:
        return {}
    if "matches" in report:
        return {"text": f"совпадений: {len(report['matches'])}"}
    analysis = report.get("analysis") or {}
    return {
        "title": (report.get("seed") or {}).get("title"),
        "index": analysis.get("index"),
        "status": analysis.get("status"),
        "status_text": analysis.get("status_text"),
    }


templates.env.globals["summary_of"] = summary_of
