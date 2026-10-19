from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from redis.asyncio import Redis
from sqlalchemy import text

from app.core.config import get_settings
from app.core.db import engine
from app.web import admin, api, api_v1, org, report, site, topics

API_DESCRIPTION = """
API «Сверки» для организаций на тарифах «Команда» и «Редакция».

**Доступ.** Ключ выпускает владелец организации в кабинете (раздел «Ключи API»).
Передавайте его в заголовке `Authorization: Bearer sv_…` или `X-API-Key: sv_…`.
Ключ показывается один раз; храните его как пароль. Отозвать ключ можно в кабинете.

**Порядок работы.** `POST /api/v1/checks` с ссылкой или текстом новости → ответ с `id`
и статусом `queued`. Затем `GET /api/v1/checks/{id}` раз в 5–10 секунд, пока статус не станет
`done` или `failed`. Проверка обычно занимает 30–90 секунд.

**Квота.** Каждая проверка расходует месячную квоту организации, как проверка с сайта или из бота.
Когда квота исчерпана, `POST` отвечает кодом 402. Остаток — `GET /api/v1/usage`.

**Ограничения.** До 30 новых проверок и 300 запросов на чтение в минуту на ключ (код 429).

**Что значит индекс.** Индекс подтверждённости (0–100) показывает, насколько широко
и независимо событие подтверждено источниками из реестра «Сверки». Это не вердикт об истинности.
"""

app = FastAPI(
    title="Сверка API",
    version="1.0",
    description=API_DESCRIPTION,
    docs_url="/api/docs",
    redoc_url=None,
    openapi_url="/api/openapi.json",
)
# Логотип и иконки (знак компании «Ангрист»).
app.mount("/static", StaticFiles(directory=Path(__file__).parent / "static"), name="static")
app.include_router(api_v1.router)
# Остальное — сайт и служебные методы бота: в документацию API не выносим.
for internal in (
    api.router,
    report.router,
    site.router,
    org.router,
    topics.router,
    admin.router,
    admin.protected,
):
    app.include_router(internal, include_in_schema=False)


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


@app.get("/health", include_in_schema=False)
async def health() -> JSONResponse:
    checks = {"db": await _check_db(), "redis": await _check_redis()}
    healthy = all(v == "ok" for v in checks.values())
    return JSONResponse(
        {"status": "ok" if healthy else "degraded", **checks},
        status_code=200 if healthy else 503,
    )
