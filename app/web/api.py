"""Публичный API: карточка источника и жалобы (для бота и веб-отчёта)."""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_session
from app.models import Check, Feedback, Source
from app.models.enums import FeedbackStatus
from app.registry.service import source_card

router = APIRouter(prefix="/api")
Session = Annotated[AsyncSession, Depends(get_session)]


async def _find_source(session: AsyncSession, handle: str) -> Source | None:
    handle = handle.strip().lower().removeprefix("https://").removeprefix("www.").rstrip("/")
    if handle.startswith("t.me/"):
        handle = "@" + handle.removeprefix("t.me/")
    return await session.scalar(select(Source).where(Source.handle_or_domain == handle))


@router.get("/sources/{handle}")
async def get_source(handle: str, session: Session) -> dict:
    """Карточка по домену (tass.ru) или каналу (@rbc_news) — команда /source в боте."""
    source = await _find_source(session, handle)
    if source is None:
        raise HTTPException(404, "источника нет в реестре")
    return await source_card(session, source)


class FeedbackIn(BaseModel):
    text: str = Field(min_length=3, max_length=4000)
    check_id: int | None = None
    source: str | None = None  # домен или @канал — апелляция источника

    @model_validator(mode="after")
    def _target(self):
        if self.check_id is None and not self.source:
            raise ValueError("нужен check_id или source")
        return self


@router.post("/feedback", status_code=201)
async def post_feedback(body: FeedbackIn, session: Session) -> dict:
    """«Сообщить об ошибке» в отчёте (ФТ-7.4) или апелляция источника (ФТ-6.4)."""
    source_id = None
    if body.source:
        source = await _find_source(session, body.source)
        if source is None:
            raise HTTPException(404, "источника нет в реестре")
        source_id = source.id
    if body.check_id is not None and await session.get(Check, body.check_id) is None:
        raise HTTPException(404, "проверки нет")
    item = Feedback(
        check_id=body.check_id, source_id=source_id, text=body.text, status=FeedbackStatus.OPEN
    )
    session.add(item)
    await session.commit()
    return {"id": item.id, "status": item.status}
