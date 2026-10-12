"""Публичный веб-отчёт по проверке: /r/<token> (ФТ-7.2). Ссылку на него присылает бот."""

import json
from datetime import datetime
from pathlib import Path
from typing import Annotated
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_session
from app.models import Check

router = APIRouter()
Session = Annotated[AsyncSession, Depends(get_session)]
templates = Jinja2Templates(directory=Path(__file__).parent / "templates")
MSK = ZoneInfo("Europe/Moscow")


def msk(iso: str) -> str:
    return datetime.fromisoformat(iso).astimezone(MSK).strftime("%d.%m.%Y %H:%M")


templates.env.filters["msk"] = msk


@router.get("/r/{token}", response_class=HTMLResponse)
async def report_page(request: Request, token: str, session: Session):
    check = await session.scalar(select(Check).where(Check.public_token == token))
    if check is None or not check.summary:
        raise HTTPException(404, "Отчёт не найден")
    return templates.TemplateResponse(
        request,
        "report.html",
        {"check": check, "report": json.loads(check.summary)},
    )
