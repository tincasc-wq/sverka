"""Кабинет: мониторинг тем организации (app/topics.py)."""

from typing import Annotated

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_session
from app.models import Topic
from app.orgs.service import membership_of
from app.topics import (
    MODES,
    hit_counts,
    is_subscribed,
    keyword_pattern,
    org_topics,
    parse_list,
    py_pattern,
    recent_groups,
    set_subscription,
    topics_limit,
)
from app.web.session import current_user
from app.web.site import page

router = APIRouter()
Session = Annotated[AsyncSession, Depends(get_session)]


async def _context(request: Request, session: AsyncSession):
    user = await current_user(request, session)
    membership = await membership_of(session, user)
    return user, membership


async def _topic(session: AsyncSession, membership, topic_id: int) -> Topic:
    topic = await session.get(Topic, topic_id)
    if topic is None or membership is None or topic.org_id != membership.org.id:
        raise HTTPException(404)
    return topic


def _form_values(
    name: str, keywords: str, exclude: str, description: str, mode: str, digest_hour: int
) -> dict:
    return {
        "name": " ".join(name.split())[:255],
        "keywords": parse_list(keywords),
        "exclude": parse_list(exclude),
        "description": " ".join(description.split())[:1000] or None,
        "mode": mode if mode in MODES else "instant",
        "digest_hour": min(23, max(0, digest_hour)),
    }


def _form_error(values: dict) -> str | None:
    if not values["name"]:
        return "Назовите тему."
    if not values["keywords"] and not values["description"]:
        return "Укажите ключевые слова или опишите тему словами — иначе искать нечего."
    return None


@router.get("/org/topics", response_class=HTMLResponse)
async def topics_page(request: Request, session: Session):
    user, membership = await _context(request, session)
    if membership is None:
        return RedirectResponse("/org", status_code=303)
    org = membership.org
    topics = await org_topics(session, org.id)
    return await page(
        request,
        session,
        "topics.html",
        user=user,
        org=org,
        topics=topics,
        counts=await hit_counts(session, [t.id for t in topics]),
        limit=topics_limit(org, user),
        modes=MODES,
        error=request.query_params.get("error"),
    )


@router.post("/org/topics")
async def topic_create(
    request: Request,
    session: Session,
    name: Annotated[str, Form()] = "",
    keywords: Annotated[str, Form()] = "",
    exclude: Annotated[str, Form()] = "",
    description: Annotated[str, Form()] = "",
    mode: Annotated[str, Form()] = "instant",
    digest_hour: Annotated[int, Form()] = 9,
):
    from urllib.parse import quote

    user, membership = await _context(request, session)
    if membership is None:
        return RedirectResponse("/org", status_code=303)
    org = membership.org
    limit = topics_limit(org, user)
    if len(await org_topics(session, org.id)) >= limit:
        message = (
            f"Достигнут предел тарифа: {limit} тем."
            if limit
            else "Мониторинг тем есть в тарифе «Мониторинг» — запросите его в кабинете."
        )
        return RedirectResponse(f"/org/topics?error={quote(message)}", status_code=303)
    values = _form_values(name, keywords, exclude, description, mode, digest_hour)
    if error := _form_error(values):
        return RedirectResponse(f"/org/topics?error={quote(error)}", status_code=303)
    topic = Topic(org_id=org.id, created_by=user.id, **values)
    session.add(topic)
    await session.flush()
    await set_subscription(session, topic, user, True)
    await session.commit()
    return RedirectResponse(f"/org/topics/{topic.id}?created=1", status_code=303)


def _highlight(topic: Topic):
    """Какие слова подсветить в заголовках находок."""
    return [py_pattern(p) for p in map(keyword_pattern, topic.keywords or []) if p]


@router.get("/org/topics/{topic_id}", response_class=HTMLResponse)
async def topic_page(request: Request, topic_id: int, session: Session):
    user, membership = await _context(request, session)
    topic = await _topic(session, membership, topic_id)
    return await page(
        request,
        session,
        "topic.html",
        user=user,
        org=membership.org,
        topic=topic,
        groups=await recent_groups(session, topic),
        mentions=await recent_groups(session, topic, prominent=False),
        subscribed=await is_subscribed(session, topic, user),
        can_edit=membership.is_owner or topic.created_by == user.id,
        modes=MODES,
        created=request.query_params.get("created"),
        error=request.query_params.get("error"),
        working=topics_limit(membership.org) > 0,
    )


@router.post("/org/topics/{topic_id}")
async def topic_update(
    request: Request,
    topic_id: int,
    session: Session,
    name: Annotated[str, Form()] = "",
    keywords: Annotated[str, Form()] = "",
    exclude: Annotated[str, Form()] = "",
    description: Annotated[str, Form()] = "",
    mode: Annotated[str, Form()] = "instant",
    digest_hour: Annotated[int, Form()] = 9,
    active: Annotated[str, Form()] = "",
):
    from urllib.parse import quote

    user, membership = await _context(request, session)
    topic = await _topic(session, membership, topic_id)
    if not (membership.is_owner or topic.created_by == user.id):
        raise HTTPException(403)
    values = _form_values(name, keywords, exclude, description, mode, digest_hour)
    if error := _form_error(values):
        return RedirectResponse(f"/org/topics/{topic.id}?error={quote(error)}", status_code=303)
    if values["description"] != topic.description:
        topic.description_vec = None  # пересчитает воркер при следующем просмотре
    for key, value in values.items():
        setattr(topic, key, value)
    topic.active = active == "on"
    await session.commit()
    return RedirectResponse(f"/org/topics/{topic.id}", status_code=303)


@router.post("/org/topics/{topic_id}/delete")
async def topic_delete(request: Request, topic_id: int, session: Session):
    user, membership = await _context(request, session)
    topic = await _topic(session, membership, topic_id)
    if not (membership.is_owner or topic.created_by == user.id):
        raise HTTPException(403)
    await session.delete(topic)
    await session.commit()
    return RedirectResponse("/org/topics", status_code=303)


@router.post("/org/topics/{topic_id}/subscribe")
async def topic_subscribe(request: Request, topic_id: int, session: Session):
    user, membership = await _context(request, session)
    topic = await _topic(session, membership, topic_id)
    await set_subscription(session, topic, user, not await is_subscribed(session, topic, user))
    await session.commit()
    return RedirectResponse(f"/org/topics/{topic.id}", status_code=303)
