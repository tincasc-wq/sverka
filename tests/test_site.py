"""Публичный сайт (этап 6): проверка с сайта от формы до отчёта, история, поиск, реестр, вход."""

import hashlib
import hmac
import json
import time
from collections.abc import AsyncIterator

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.db import engine
from app.models import Check, Document, Feedback, Source, User
from app.web import site
from app.web.checks import CheckRefused, create_web_check, run_web_check
from app.web.main import app
from app.web.session import COOKIE, sign, telegram_login_valid, unsign
from tests.test_embeddings import FakeEmbedder
from tests.test_story import vec

CLEANUP = (
    "org_invites",
    "org_members",
    "feedback",
    "claim_evidence",
    "claims",
    "llm_calls",
    "checks",
    "story_documents",
    "derivations",
    "stories",
    "documents",
    "sources",
    "users",
    "organizations",
)


@pytest.fixture
async def db() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    maker = async_sessionmaker(engine, expire_on_commit=False)
    yield maker
    async with engine.begin() as conn:
        await conn.exec_driver_sql("SET session_replication_role = replica")
        for table in CLEANUP:
            await conn.exec_driver_sql(f"DELETE FROM {table}")
        await conn.exec_driver_sql("SET session_replication_role = DEFAULT")


@pytest.fixture
def queued(monkeypatch) -> list[tuple]:
    jobs: list[tuple] = []

    async def fake_enqueue(job: str, *args) -> None:
        jobs.append((job, *args))

    monkeypatch.setattr(site, "enqueue", fake_enqueue)
    return jobs


def client() -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://t")


async def _archive(db) -> tuple[Source, Document]:
    async with db() as s:
        bbc = Source(
            type="media",
            name="BBC News",
            handle_or_domain="bbc.com",
            lang="en",
            legal_flags={"blocked_in_ru": "таймаут из РФ"},
        )
        s.add(bbc)
        await s.flush()
        doc = Document(
            url="https://bbc.com/news/1",
            title="Пожар на складе в Подольске",
            text="Склад площадью 2000 квадратных метров загорелся утром в Подольске.",
            embedding=vec(1.0),
            source_id=bbc.id,
        )
        s.add(doc)
        await s.commit()
    return bbc, doc


async def test_check_from_site_goes_from_form_to_report(db, queued) -> None:
    await _archive(db)
    async with client() as c:
        home = await c.get("/")
        started = await c.post("/check", data={"value": "Смотрите https://bbc.com/news/1 !"})
        token = started.headers["location"].removeprefix("/c/")
        waiting = await c.get(f"/c/{token}")
        status = (await c.get(f"/c/{token}/status")).json()

        [(job, check_id, use_cache)] = queued
        async with db() as s:
            done = await run_web_check(s, check_id, FakeEmbedder(), None)
        assert done.status == "done" and done.document_id is not None

        redirected = await c.get(f"/c/{token}")
        report = await c.get(f"/r/{token}")
        history = await c.get("/history")

    assert home.status_code == 200 and "Проверить" in home.text
    assert started.status_code == 303 and COOKIE in started.cookies
    assert (job, use_cache) == ("web_check", True)
    assert waiting.status_code == 200 and "Проверяю" in waiting.text
    assert status == {"status": "queued"}
    assert redirected.headers["location"] == f"/r/{token}"
    assert report.status_code == 200
    assert "BBC News (заблокировано в РФ)" in report.text  # правовая пометка в отчёте
    assert "Нашли ошибку в отчёте?" in report.text
    assert "Пожар на складе" in history.text


async def test_text_check_shows_earliest_matches(db, queued) -> None:
    await _archive(db)
    text = (
        "Склад площадью две тысячи метров загорелся утром в Подольске, сообщают очевидцы на месте."
    )
    async with client() as c:
        started = await c.post("/check", data={"value": text})
        token = started.headers["location"].removeprefix("/c/")
        async with db() as s:
            await run_web_check(s, queued[0][1], FakeEmbedder(), None)
        report = await c.get(f"/r/{token}")

    assert report.status_code == 200
    assert "Где и когда появлялся этот текст" in report.text
    assert "https://bbc.com/news/1" in report.text


async def test_unreachable_link_fails_with_reason(db, queued) -> None:
    async with client() as c:
        started = await c.post("/check", data={"value": "http://127.0.0.1/secret"})
        token = started.headers["location"].removeprefix("/c/")
        async with db() as s:
            failed = await run_web_check(s, queued[0][1], FakeEmbedder(), None)
        page = await c.get(f"/c/{token}")
        status = (await c.get(f"/c/{token}/status")).json()

    assert failed.status == "failed"
    assert "недоступный адрес" in page.text and status["status"] == "failed"


async def test_nonsense_input_and_quota_are_refused(db, queued) -> None:
    async with client() as c:
        nonsense = await c.post("/check", data={"value": "ага"})
    assert nonsense.status_code == 422 and "Не понял" in nonsense.text
    assert queued == []

    async with db() as s:
        user = User(daily_quota=0)
        s.add(user)
        await s.flush()
        with pytest.raises(CheckRefused, match="Войдите через Telegram"):
            await create_web_check(s, user, "https://bbc.com/news/1")


async def test_history_is_per_browser(db, queued) -> None:
    async with client() as mine:
        await mine.post("/check", data={"value": "https://bbc.com/news/1"})
        own = await mine.get("/history")
    async with client() as other:
        foreign = await other.get("/history")

    assert "bbc.com/news/1" in own.text
    assert "bbc.com/news/1" not in foreign.text and "Проверок пока нет" in foreign.text


async def test_search_registry_and_methodology(db) -> None:
    bbc, _ = await _archive(db)
    async with client() as c:
        found = await c.get("/search", params={"q": "подольске"})
        empty = await c.get("/search", params={"q": "марсоход"})
        registry = await c.get("/sources", params={"lang": "en"})
        card = await c.get(f"/sources/{bbc.id}")
        method = await c.get("/methodology")

    assert "Пожар на складе в Подольске" in found.text and "Проверить эту публикацию" in found.text
    assert "Ничего не нашлось" in empty.text
    assert "BBC News (заблокировано в РФ)" in registry.text and "мало данных" in registry.text
    assert card.status_code == 200 and "Последние публикации" in card.text
    assert method.status_code == 200 and "широко подтверждено" in method.text


async def test_feedback_from_report_reaches_analyst(db) -> None:
    async with db() as s:
        check = Check(
            input_type="text",
            input_raw="текст",
            status="done",
            public_token="tok-fb",
            summary=json.dumps({"matches": [], "days": 7}),
        )
        s.add(check)
        await s.commit()
    async with client() as c:
        sent = await c.post("/r/tok-fb/feedback", data={"text": "Чужое событие в истории"})
        page = await c.get(sent.headers["location"])

    async with db() as s:
        [item] = (await s.scalars(select(Feedback))).all()
    assert item.check_id == check.id and item.text == "Чужое событие в истории"
    assert "Спасибо, передали аналитику" in page.text


def test_session_cookie_cannot_be_forged() -> None:
    assert unsign(sign(42)) == 42
    assert unsign("43." + sign(42).split(".")[1]) is None
    assert unsign("garbage") is None


def _telegram_payload(bot_token: str, **fields) -> dict[str, str]:
    data = {"id": "777", "first_name": "Т", "auth_date": str(int(time.time())), **fields}
    check = "\n".join(f"{k}={v}" for k, v in sorted(data.items()))
    secret = hashlib.sha256(bot_token.encode()).digest()
    data["hash"] = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    return data


async def test_telegram_login_links_browser_history(db, queued, monkeypatch) -> None:
    from app.core.config import get_settings

    monkeypatch.setattr(get_settings(), "telegram_bot_token", "123:abc")
    payload = _telegram_payload("123:abc")
    assert telegram_login_valid(dict(payload), "123:abc")
    assert not telegram_login_valid({**payload, "id": "778"}, "123:abc")
    assert not telegram_login_valid(_telegram_payload("123:abc", auth_date="1"), "123:abc")

    async with client() as c:
        await c.post("/check", data={"value": "https://bbc.com/news/1"})
        logged = await c.get("/auth/telegram", params=payload)
        history = await c.get("/history")
        forged = await c.get("/auth/telegram", params={**payload, "hash": "0" * 64})

    async with db() as s:
        tg_user = await s.scalar(select(User).where(User.tg_id == 777))
        owners = set(await s.scalars(select(Check.user_id)))
    assert logged.headers["location"] == "/history"
    assert owners == {tg_user.id}  # анонимная проверка перешла к пользователю Telegram
    assert "bbc.com/news/1" in history.text and "Выйти" in history.text
    assert forged.headers["location"] == "/login?error=1"
