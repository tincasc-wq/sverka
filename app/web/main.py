from fastapi import FastAPI
from fastapi.responses import JSONResponse
from redis.asyncio import Redis
from sqlalchemy import text

from app.core.config import get_settings
from app.core.db import engine
from app.web import admin, api, report

app = FastAPI(title="Сверка", version="0.1.0")
app.include_router(api.router)
app.include_router(report.router)
app.include_router(admin.router)
app.include_router(admin.protected)


async def _check_db() -> str:
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        return "ok"
    except Exception as exc:  # noqa: BLE001 — для health важен любой отказ
        return f"error: {type(exc).__name__}"


async def _check_redis() -> str:
    client = Redis.from_url(get_settings().redis_url)
    try:
        await client.ping()
        return "ok"
    except Exception as exc:  # noqa: BLE001
        return f"error: {type(exc).__name__}"
    finally:
        await client.aclose()


@app.get("/health")
async def health() -> JSONResponse:
    checks = {"db": await _check_db(), "redis": await _check_redis()}
    healthy = all(v == "ok" for v in checks.values())
    return JSONResponse(
        {"status": "ok" if healthy else "degraded", **checks},
        status_code=200 if healthy else 503,
    )
