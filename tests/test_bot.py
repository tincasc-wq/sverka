import json
from datetime import UTC, datetime

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot import texts
from app.bot.inputs import extract_input
from app.bot.service import check_for_chat, checks_today, get_or_create_user, quota_exceeded
from app.core.db import SessionLocal
from app.models import Check, Document, Source
from app.models.enums import InputType
from app.web.main import app
from tests.test_embeddings import FakeEmbedder
from tests.test_story import vec


@pytest.mark.parametrize(
    ("text", "channel", "msg_id", "expected"),
    [
        (
            "Глянь https://tass.ru/obschestvo/123?utm_source=tg.",
            None,
            None,
            (InputType.URL, "https://tass.ru/obschestvo/123?utm_source=tg"),
        ),
        (
            "Текст поста из канала",
            "breakingmash",
            555,
            (InputType.TG_FORWARD, "https://t.me/breakingmash/555"),
        ),
        (
            "Говорят, что в Подмосковье с понедельника закроют все школы на карантин",
            None,
            None,
            (
                InputType.TEXT,
                "Говорят, что в Подмосковье с понедельника закроют все школы на карантин",
            ),
        ),
        ("правда?", None, None, None),
        (None, None, None, None),
    ],
)
def test_extract_input(text, channel, msg_id, expected) -> None:
    result = extract_input(text, channel, msg_id)
    assert (None if result is None else (result.type, result.value)) == expected


REPORT = {
    "seed": {"url": "https://rbc.ru/1", "title": "Путин ввёл <безвиз> для Вьетнама"},
    "publications": 21,
    "independent_reports": 3,
    "primary": {
        "url": "https://kremlin.ru/acts/news/1",
        "source": "Президент России",
        "time": "2026-10-12T11:00:00+00:00",
        "membership": "probable",
        "notes": ["Принадлежность к событию не подтверждена"],
    },
    "timeline": [
        {
            "time": "2026-10-12T11:00:00+00:00",
            "source": "Президент России",
            "membership": "probable",
            "role": "primary",
        },
        {
            "time": "2026-10-12T11:53:00+00:00",
            "source": "Москва 24",
            "membership": "core",
            "role": "derived",
        },
        {
            "time": "2026-10-12T11:55:00+00:00",
            "source": "РБК",
            "membership": "core",
            "role": "derived",
        },
    ],
}


def test_summary_is_short_escaped_and_has_disclaimer() -> None:
    card = {"name": "РБК", "rating": {"score": 70, "ci": [50, 85], "shown": False, "outcomes": 2}}

    text = texts.format_check(REPORT, card, "http://localhost:8000/r/abc")

    assert "&lt;безвиз&gt;" in text  # заголовок из чужого текста экранирован
    assert "Президент России" in text and "14:00" in text  # время по Москве
    assert "недостаточно истории" in text
    assert "не является утверждением" in text
    assert len(text) < 1500


async def test_daily_quota(session: AsyncSession) -> None:
    user = await get_or_create_user(session, tg_id=111)
    user.daily_quota = 2
    for i in range(2):
        session.add(Check(user_id=user.id, input_type="url", input_raw=f"u{i}", status="done"))
    await session.flush()

    assert await checks_today(session, user) == 2
    assert await quota_exceeded(session, user)
    assert (await get_or_create_user(session, tg_id=111)).id == user.id


async def test_check_for_chat_from_archive_document(session: AsyncSession) -> None:
    src = Source(type="media", name="РБК", handle_or_domain="rbc.ru")
    session.add(src)
    await session.flush()
    doc = Document(
        url="https://rbc.ru/1",
        title="Новость",
        text="Текст",
        source_id=src.id,
        embedding=vec(1.0),
        published_at=datetime(2026, 10, 12, tzinfo=UTC),
    )
    session.add(doc)
    await session.flush()

    reply = await check_for_chat(session, "https://rbc.ru/1", InputType.URL, 222, FakeEmbedder())

    assert reply.check_id is not None
    assert "Новость" in reply.text
    assert "/r/" in reply.text
    check = await session.get(Check, reply.check_id)
    assert check.user_id == (await get_or_create_user(session, 222)).id


async def test_unsafe_url_gives_friendly_error(session: AsyncSession) -> None:
    reply = await check_for_chat(session, "http://127.0.0.1/x", InputType.URL, 333, FakeEmbedder())

    assert reply.check_id is None
    assert reply.text.startswith("Не получилось проверить")


async def test_text_search_lists_earliest_matches(session: AsyncSession) -> None:
    session.add(
        Document(
            url="https://a.ru/1",
            title="Школы закроют",
            text="x",
            embedding=[float(len("Говорят, что школы закроют на карантин с понедельника"))]
            + [0.0] * 1023,
        )
    )
    await session.flush()

    reply = await check_for_chat(
        session,
        "Говорят, что школы закроют на карантин с понедельника",
        InputType.TEXT,
        444,
        FakeEmbedder(),
    )

    assert "Школы закроют" in reply.text
    assert reply.check_id is None


async def test_public_report_page() -> None:
    # Веб-приложение читает своим подключением — отчёт должен быть закоммичен по-настоящему.
    check = Check(
        input_type="url",
        input_raw="x",
        status="done",
        public_token="tok123",
        summary=json.dumps(
            {
                **REPORT,
                "borderline": [],
                "note": "Без LLM",
                "timeline": [
                    {
                        **t,
                        "url": "https://e.ru",
                        "title": "Т",
                        "date_unreliable": False,
                        "took_from": [],
                        "attributes_outside_archive": [],
                    }
                    for t in REPORT["timeline"]
                ],
            }
        ),
        finished_at=datetime(2026, 10, 12, tzinfo=UTC),
    )
    async with SessionLocal() as s:
        s.add(check)
        await s.commit()
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            ok = await c.get("/r/tok123")
            missing = await c.get("/r/nope")
        assert ok.status_code == 200
        assert "Президент России" in ok.text and "Истории" not in ok.text  # без меню админки
        assert missing.status_code == 404
    finally:
        async with SessionLocal() as s:
            await s.execute(delete(Check).where(Check.public_token == "tok123"))
            await s.commit()
