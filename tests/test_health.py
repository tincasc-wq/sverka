from httpx import ASGITransport, AsyncClient

from app.web.main import app


async def test_health_reports_db_and_redis_ok() -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "db": "ok", "redis": "ok"}
