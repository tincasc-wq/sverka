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

    core_primary = {**REPORT["primary"], "membership": "core", "notes": []}
    text = texts.format_check(
        {**REPORT, "primary": core_primary}, card, "http://localhost:8000/r/abc"
    )

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


def test_probable_primary_is_not_called_primary() -> None:
    report = {
        **REPORT,
        "core_publications": 1,
        "publications": 5,
        "primary": {
            **REPORT["primary"],
            "earliest_core_root": {"url": "https://rbc.ru/1", "source": "РБК"},
        },
        "timeline": [
            {**REPORT["timeline"][0], "url": "https://kremlin.ru/1", "title": "Ранее по теме"},
            {
                "time": "2026-10-12T12:00:00+00:00",
                "source": "@vzglyad_ru",
                "membership": "core",
                "role": "independent",
                "is_seed": True,
                "url": "https://rbc.ru/1",
                "attributes_outside_archive": ["Wyborcza"],
            },
        ],
        "earlier_related": [
            {
                "time": "2026-10-12T11:00:00+00:00",
                "source": "Коммерсантъ",
                "url": "https://k.ru/1",
                "title": "МВД Польши заявило",
            }
        ],
    }

    text = texts.format_check(report, None, "http://x/r/1", "@vzglyad_ru")

    assert "Первоисточник" not in text
    assert "Более ранних публикаций именно об этом в архиве нет" in text
    assert "ссылается на: Wyborcza. Исходной публикации в архиве нет" in text
    assert "и ещё 4 на ту же тему" in text
    assert "Раньше по той же теме" in text and "Коммерсантъ" in text
    assert "@vzglyad_ru нет в реестре" in text


async def test_link_with_unknown_tracking_param_finds_archived_article(
    session: AsyncSession,
) -> None:
    src = Source(type="media", name="Лента.ру", handle_or_domain="lenta.ru")
    session.add(src)
    await session.flush()
    archived = Document(
        url="https://lenta.ru/news/2026/10/12/teror",
        title="Новость",
        text="Текст",
        source_id=src.id,
        embedding=vec(1.0),
    )
    session.add(archived)
    await session.flush()

    reply = await check_for_chat(
        session,
        "https://lenta.ru/news/2026/10/12/teror/?somenewclid=abc",
        InputType.URL,
        555,
        FakeEmbedder(),
    )

    check = await session.get(Check, reply.check_id)
    assert check.document_id == archived.id  # не скачали заново как «другую» статью


def test_seed_citing_registry_source_is_not_called_primary() -> None:
    seed = {
        "time": "2026-10-12T00:00:00+00:00",
        "source": "Лента.ру",
        "membership": "core",
        "role": "primary",
        "is_seed": True,
        "url": "https://lenta.ru/1",
        "attributes_outside_archive": ["Комсомольская правда"],
    }
    report = {
        **REPORT,
        "seed": {"url": "https://lenta.ru/1", "title": "Т"},
        "core_publications": 1,
        "publications": 1,
        "independent_core": 1,
        "timeline": [seed],
        "primary": {
            "url": "https://lenta.ru/1",
            "source": "Лента.ру",
            "membership": "core",
            "time": "2026-10-12T00:00:00+00:00",
            "notes": [],
        },
    }

    text = texts.format_check(report, None, "http://x/r/1")

    assert "Первоисточник:" not in text
    assert "Самая ранняя публикация в архиве — эта" in text
    assert "Она ссылается на: Комсомольская правда" in text


def test_forward_carries_text_without_promo_footer() -> None:
    caption = (
        "Сотрудники ТЦК в Харькове затолкали мужчину в микроавтобус, пишут в местных каналах.\n\n"
        "🟩 Подписаться на RT: ТГ | Зеркало | MAX"
    )

    result = extract_input(caption, "rt_russian", 295049, "2026-10-12T19:05:00+00:00")

    assert result.value == "https://t.me/rt_russian/295049"
    assert result.text.endswith("пишут в местных каналах.")
    assert result.published_at == "2026-10-12T19:05:00+00:00"


def test_invite_links_are_not_checked() -> None:
    text = "Подписаться: https://t.me/+ELcA6UUZf0w3Y2Uy а новость тут https://ria.ru/2026/1.html"

    assert extract_input(text).value == "https://ria.ru/2026/1.html"


async def test_blocked_post_is_checked_by_forwarded_text(session: AsyncSession) -> None:
    reply = await check_for_chat(
        session,
        "https://t.me/rt_russian/1",  # без прокси t.me не откроется, как скрытый канал
        InputType.TG_FORWARD,
        666,
        FakeEmbedder(),
        fallback_text="Сотрудники ТЦК в Харькове затолкали мужчину в микроавтобус.",
        fallback_date="2026-10-12T19:05:00+00:00",
    )

    assert reply.check_id is not None
    check = await session.get(Check, reply.check_id)
    doc = await session.get(Document, check.document_id)
    assert doc.url == "https://t.me/rt_russian/1"
    assert doc.text.startswith("Сотрудники ТЦК")
    assert doc.published_at == datetime(2026, 10, 12, 19, 5, tzinfo=UTC)


def test_check_shows_confirmation_index_and_claims() -> None:
    analysis = {
        "index": 41.5,
        "status": "not_confirmed",
        "status_text": "не подтверждено",
        "claims": [
            {
                "text": "Склад <площадью> 2000 кв. м",
                "checkable": True,
                "status": "not_confirmed",
                "evidence": [
                    {
                        "source": "Интерфакс",
                        "stance": "contradicts",
                        "independent": True,
                        "low_confidence": False,
                        "discrepancy": "3000 вместо 2000",
                    }
                ],
            },
            {"text": "Это позор", "checkable": False, "status": "refuted", "evidence": []},
        ],
    }

    text = texts.format_check({**REPORT, "analysis": analysis}, None, "http://x/r/1")
    failed = texts.format_check({**REPORT, "analysis": {"error": "x"}}, None, "http://x/r/1")

    assert "Индекс подтверждённости: 41.5 из 100" in text
    assert "&lt;площадью&gt;" in text and "противоречит Интерфакс: 3000 вместо 2000" in text
    assert "Это позор" not in text  # мнения в краткий ответ не попадают
    assert "языковая модель недоступна" in failed
