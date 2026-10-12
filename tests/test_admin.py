from collections.abc import AsyncIterator

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.db import engine
from app.models import Document, Feedback, Outcome, Source, SourceScore, Story, StoryDocument
from app.web.auth import COOKIE
from app.web.main import app

TOKEN = "test-admin-token"
CLEANUP = (
    "feedback",
    "outcomes",
    "source_scores",
    "story_documents",
    "derivations",
    "checks",
    "documents",
    "stories",
    "sources",
)


@pytest.fixture
async def db() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """Веб-слой коммитит сам — работаем с настоящими транзакциями и чистим после."""
    maker = async_sessionmaker(engine, expire_on_commit=False)
    yield maker
    # outcomes и source_scores защищены триггером от удаления — в тестовой базе чистим
    # в режиме replica (триггеры, включая проверки внешних ключей, отключены).
    async with engine.begin() as conn:
        await conn.exec_driver_sql("SET session_replication_role = replica")
        for table in CLEANUP:
            await conn.exec_driver_sql(f"DELETE FROM {table}")
        await conn.exec_driver_sql("SET session_replication_role = DEFAULT")


def client(authorized: bool = True) -> AsyncClient:
    return AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={COOKIE: TOKEN} if authorized else None,
    )


async def test_admin_requires_login() -> None:
    async with client(authorized=False) as c:
        response = await c.get("/admin/sources")
        bad_login = await c.post("/admin/login", data={"token": "wrong"})
        good_login = await c.post("/admin/login", data={"token": TOKEN})

    assert response.status_code == 303
    assert response.headers["location"] == "/admin/login"
    assert bad_login.status_code == 401
    assert good_login.status_code == 303
    assert COOKIE in good_login.cookies


async def test_analyst_marks_story_in_one_form(db) -> None:
    async with db() as s:
        fake = Source(type="tg", name="Фейкоканал", handle_or_domain="@fake")
        check = Source(type="media", name="Проверка", handle_or_domain="check.ru")
        s.add_all([fake, check])
        await s.flush()
        story = Story(title="Взрыв на заводе")
        s.add(story)
        await s.flush()
        d1 = Document(url="https://t.me/fake/1", source_id=fake.id, title="Взрыв!")
        d2 = Document(url="https://check.ru/1", source_id=check.id, title="Взрыва не было")
        s.add_all([d1, d2])
        await s.flush()
        s.add_all(
            [
                StoryDocument(story_id=story.id, document_id=d1.id),
                StoryDocument(story_id=story.id, document_id=d2.id),
            ]
        )
        await s.commit()

    async with client() as c:
        page = await c.get(f"/admin/stories/{story.id}")
        marked = await c.post(
            f"/admin/stories/{story.id}/outcome",
            data={
                "verdict": "refuted",
                "evidence_url": "https://mchs.gov.ru/1",
                f"stance_{d1.id}": "supports",
                f"stance_{d2.id}": "contradicts",
            },
        )

    assert page.status_code == 200
    assert "Взрыв на заводе" in page.text
    assert marked.status_code == 303
    async with db() as s:
        outcome = await s.scalar(select(Outcome))
        assert (outcome.verdict, outcome.decided_by) == ("refuted", "analyst")
        link = await s.get(StoryDocument, (story.id, d2.id))
        assert link.analyst_stance == "contradicts"


async def test_source_card_api_and_admin_page(db) -> None:
    async with db() as s:
        src = Source(type="agency", name="ТАСС", handle_or_domain="tass.ru", owner="ТАСС")
        s.add(src)
        await s.flush()
        s.add(
            SourceScore(
                source_id=src.id,
                score=75,
                ci_low=55,
                ci_high=90,
                n_observations=3,
                reason="Стартовая оценка",
            )
        )
        await s.commit()

    async with client() as c:
        api = await c.get("/api/sources/www.tass.ru")
        missing = await c.get("/api/sources/nope.ru")
        page = await c.get(f"/admin/sources/{src.id}")
        listing = await c.get("/admin/sources")

    card = api.json()
    assert card["rating"]["score"] == 75
    assert card["rating"]["shown"] is False  # 3 исхода < 10
    assert missing.status_code == 404
    assert page.status_code == 200 and "Стартовая оценка" in page.text
    assert listing.status_code == 200 and "ТАСС" in listing.text


async def test_feedback_from_report_and_resolution(db) -> None:
    async with db() as s:
        s.add(Source(type="media", name="Издание", handle_or_domain="paper.ru"))
        await s.commit()

    async with client(authorized=False) as c:
        created = await c.post(
            "/api/feedback",
            json={"source": "paper.ru", "text": "Рейтинг занижен, вот опровержение"},
        )
        invalid = await c.post("/api/feedback", json={"text": "ни к чему не привязано"})
    async with client() as c:
        listing = await c.get("/admin/feedback")
        resolved = await c.post(
            f"/admin/feedback/{created.json()['id']}",
            data={"status": "resolved", "resolution": "ok"},
        )

    assert created.status_code == 201
    assert invalid.status_code == 422
    assert "Рейтинг занижен" in listing.text
    assert resolved.status_code == 303
    async with db() as s:
        assert (await s.scalar(select(Feedback))).status == "resolved"
