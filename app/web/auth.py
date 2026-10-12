"""Вход в админку по токену из .env (ADMIN_TOKEN).

Временная схема для локальной работы; по ТЗ (раздел 9) — Telegram Login, появится с ботом (этап 4).
"""

import hmac

from fastapi import HTTPException, Request, status

from app.core.config import get_settings

COOKIE = "sverka_admin"


def token_valid(token: str | None) -> bool:
    expected = get_settings().admin_token
    return bool(expected) and bool(token) and hmac.compare_digest(token, expected)


async def require_admin(request: Request) -> None:
    if not token_valid(request.cookies.get(COOKIE)):
        raise HTTPException(
            status_code=status.HTTP_303_SEE_OTHER, headers={"Location": "/admin/login"}
        )
