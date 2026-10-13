"""Доступ в админку: учётная запись сайта с ролью admin/analyst или токен ADMIN_TOKEN.

Основной путь — войти на сайт (почта или Telegram) под учётной записью с нужной ролью
(users.role). Токен остаётся запасным входом на случай, если вход на сайт недоступен.
"""

import hmac
from typing import Annotated

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.db import get_session
from app.models.enums import UserRole

COOKIE = "sverka_admin"
STAFF_ROLES = {UserRole.ADMIN.value, UserRole.ANALYST.value}


def token_valid(token: str | None) -> bool:
    expected = get_settings().admin_token
    return bool(expected) and bool(token) and hmac.compare_digest(token, expected)


def is_staff(user) -> bool:
    return user is not None and user.role in STAFF_ROLES


async def require_admin(
    request: Request, session: Annotated[AsyncSession, Depends(get_session)]
) -> None:
    if token_valid(request.cookies.get(COOKIE)):
        return
    from app.web.session import current_user

    if is_staff(await current_user(request, session)):
        return
    raise HTTPException(status_code=status.HTTP_303_SEE_OTHER, headers={"Location": "/admin/login"})
