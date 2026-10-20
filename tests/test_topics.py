"""Мониторинг тем: подбор слов, находки в архиве, сюжеты, оповещения и кабинет."""

from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select

from app.models import Document, Organization, OrgMember, Source, Topic, TopicHit, User
from app.topics import (
    Item,
    deliver,
    group_stories,
    keyword_pattern,
    parse_list,
    py_pattern,
    run_topics,
    scan_topic,
    set_subscription,
)
from app.web.session import COOKIE, sign
from tests.test_site import client as make_client
from tests.test_site import db  # noqa: F401 — фикстура
from tests.test_watches import FakeBot

NOW = datetime.now(UTC)


def _vec(*head: float) -> list[float]:
    v = [0.0] * 1024
    v[: len(head)] = head
    return v


def test_keywords_match_word_forms() -> None:
    moscow = py_pattern(keyword_pattern("Москва"))
    assert moscow.search("Пожар в Москве ночью")
    assert moscow.search("мэрия москвы сообщила")
    assert not moscow.search("Подмосковье")  # слово должно начинаться с основы
    gazprom = py_pattern(keyword_pattern("Газпром нефть"))
    assert gazprom.search("акции «Газпром нефти» выросли")
    assert not gazprom.search("Газпром и нефть")  # фраза — слова подряд
    assert py_pattern(keyword_pattern("МЧС")).search("Сотрудники МЧС прибыли")
    assert keyword_pattern("  , ") is None


def test_parse_list() -> None:
    assert parse_list("Газпром, Миллер;  газпром\nГазпром   нефть, ,") == [
        "Газпром",
        "Миллер",
        "Газпром нефть",
    ]


def test_group_stories_names_first_source() -> None:
    def item(name: str, minutes: int, vec: list[float]) -> Item:
        doc = Document(url=f"https://{name}.ru/1", title=name, embedding=vec)
        return Item(doc, name, NOW + timedelta(minutes=minutes))

    fire = [
        item("ТАСС", 5, _vec(1, 0.1)),
        item("РИА", 0, _vec(1, 0)),
        item("РБК", 9, _vec(1, 0.05)),
    ]
    other = [item("Коммерсантъ", 3, _vec(0, 1))]
    groups = group_stories(fire + other)
    assert len(groups) == 2
    assert groups[0].first.source == "РИА"  # написал раньше всех
    assert groups[0].sources == ["РБК", "РИА", "ТАСС"]


async def _org_with_topic(db, **topic_kw) -> tuple[int, int, int]:  # noqa: F811
    async with db() as s:
        org = Organization(name="Редакция", plan="monitoring", seats=5, monthly_quota=100)
        user = User(tg_id=555, email="ed@example.ru")
        s.add_all([org, user])
        await s.flush()
        s.add(OrgMember(org_id=org.id, user_id=user.id, role="owner"))
        topic = Topic(org_id=org.id, created_by=user.id, name="Газпром", **topic_kw)
        s.add(topic)
        await s.flush()
        await set_subscription(s, topic, user, True)
        await s.commit()
        return org.id, user.id, topic.id


async def _docs(db, rows: list[tuple[str, str, int, list[float] | None]]) -> None:  # noqa: F811
    async with db() as s:
        tass = await s.scalar(select(Source).where(Source.handle_or_domain == "tass.ru"))
        if tass is None:
            tass = Source(type="agency", name="ТАСС", handle_or_domain="tass.ru")
            s.add(tass)
            await s.flush()
        for url, title, minutes_ago, vec in rows:
            s.add(
                Document(
                    url=url,
                    source_id=tass.id,
                    title=title,
                    text=title,
                    first_seen_at=NOW - timedelta(minutes=minutes_ago),
                    embedding=vec,
                )
            )
        await s.commit()


async def test_scan_finds_keywords_and_meaning_and_skips_excluded(db) -> None:  # noqa: F811
    _, _, topic_id = await _org_with_topic(
        db,
        keywords=["Газпром"],
        exclude=["Зенит"],
        description="газ в Европу",
        description_vec=_vec(1, 0),
        scanned_until=NOW - timedelta(hours=1),
    )
    await _docs(
        db,
        [
            ("https://tass.ru/1", "Газпрома акции растут", 30, _vec(0, 1)),
            ("https://tass.ru/2", "Зенит и Газпром продлили контракт", 25, _vec(0, 1)),
            ("https://tass.ru/3", "Поставки топлива в Европу", 20, _vec(1, 0.1)),
            ("https://tass.ru/4", "Погода в Москве", 15, _vec(0, 1)),
            ("https://tass.ru/5", "Газпром: старое", 120, _vec(0, 1)),  # до прошлого просмотра
        ],
    )
    async with db() as s:
        topic = await s.get(Topic, topic_id)
        added = await scan_topic(s, topic, None, NOW)
        await s.commit()
        hits = {
            url: matched
            for url, matched in await s.execute(
                select(Document.url, TopicHit.matched).join(
                    TopicHit, TopicHit.document_id == Document.id
                )
            )
        }
    assert added == 2
    assert hits == {"https://tass.ru/1": "keyword", "https://tass.ru/3": "meaning"}
    async with db() as s:  # повторный просмотр ничего не дублирует
        assert await scan_topic(s, await s.get(Topic, topic_id), None, NOW) == 0


async def test_first_scan_shows_day_without_notifying(db) -> None:  # noqa: F811
    _, _, topic_id = await _org_with_topic(db, keywords=["Газпром"])
    await _docs(db, [("https://tass.ru/1", "Газпром отчитался", 300, _vec(1))])
    bot = FakeBot()
    async with db() as s:
        topic = await s.get(Topic, topic_id)
        assert await scan_topic(s, topic, None, NOW) == 1
        await s.commit()
        assert await deliver(s, topic, NOW, bot) == 0
    assert bot.sent == []


async def test_instant_notice_names_first_and_respects_pause(db) -> None:  # noqa: F811
    _, _, topic_id = await _org_with_topic(
        db, keywords=["Газпром"], scanned_until=NOW - timedelta(hours=1)
    )
    await _docs(
        db,
        [
            ("https://tass.ru/1", "Газпром снизил цены", 40, _vec(1)),
            ("https://tass.ru/2", "Газпром снизил цены для Европы", 30, _vec(1, 0.01)),
        ],
    )
    bot = FakeBot()
    stats = await run_topics(db, None, bot, now=NOW)
    assert stats["hits"] == 2 and stats["sent"] == 1
    chat, html = bot.sent[0]
    assert chat == 555
    assert "Новое по теме «Газпром»" in html and "сюжетов: 1" in html and "первым: ТАСС" in html
    await _docs(db, [])  # новых публикаций нет — второй раз не пишем
    assert (await run_topics(db, None, bot, now=NOW + timedelta(minutes=10)))["sent"] == 0


async def test_digest_waits_for_its_hour(db) -> None:  # noqa: F811
    from app.plans import MSK

    local = NOW.astimezone(MSK)
    other_hour = (local.hour + 3) % 24
    _, _, topic_id = await _org_with_topic(
        db,
        keywords=["Газпром"],
        mode="digest",
        digest_hour=other_hour,
        scanned_until=NOW - timedelta(hours=1),
    )
    await _docs(db, [("https://tass.ru/1", "Газпром снизил цены", 40, _vec(1))])
    bot = FakeBot()
    assert (await run_topics(db, None, bot, now=NOW))["sent"] == 0
    async with db() as s:
        topic = await s.get(Topic, topic_id)
        topic.digest_hour = local.hour
        await s.commit()
    assert (await run_topics(db, None, bot, now=NOW))["sent"] == 1
    assert "Сводка по теме" in bot.sent[0][1]


async def test_plan_without_monitoring_does_not_scan(db) -> None:  # noqa: F811
    org_id, _, _ = await _org_with_topic(
        db, keywords=["Газпром"], scanned_until=NOW - timedelta(hours=1)
    )
    async with db() as s:
        (await s.get(Organization, org_id)).plan = "editorial"
        await s.commit()
    await _docs(db, [("https://tass.ru/1", "Газпром снизил цены", 40, _vec(1))])
    assert (await run_topics(db, None, FakeBot(), now=NOW))["topics"] == 0


async def test_cabinet_creates_topic_within_plan(db) -> None:  # noqa: F811
    client = make_client()
    org_id, user_id, _ = await _org_with_topic(db, keywords=["Газпром"])
    client.cookies.set(COOKIE, sign(user_id))
    response = await client.post(
        "/org/topics",
        data={
            "name": "Минфин",
            "keywords": "Минфин, Силуанов",
            "mode": "digest",
            "digest_hour": "8",
        },
    )
    assert response.status_code == 303
    async with db() as s:
        topic = await s.scalar(select(Topic).where(Topic.name == "Минфин"))
        assert topic.keywords == ["Минфин", "Силуанов"] and topic.mode == "digest"
    page = await client.get(f"/org/topics/{topic.id}")
    assert page.status_code == 200 and "Минфин" in page.text
    listing = await client.get("/org/topics")
    assert "Газпром" in listing.text and "использовано 2 из 20" in listing.text
    empty = await client.post("/org/topics", data={"name": "Пусто"})
    assert "error=" in empty.headers["location"]
    async with db() as s:  # «Редакция» — без мониторинга: создать тему нельзя
        (await s.get(Organization, org_id)).plan = "editorial"
        await s.commit()
    refused = await client.post("/org/topics", data={"name": "X", "keywords": "x"})
    assert "error=" in refused.headers["location"]
    async with db() as s:
        assert await s.scalar(select(func.count()).select_from(Topic)) == 2


async def test_passing_mention_is_not_notified(db) -> None:  # noqa: F811
    _, _, topic_id = await _org_with_topic(
        db, keywords=["Газпром"], scanned_until=NOW - timedelta(hours=1)
    )
    filler = "Фестиваль собрал тысячи зрителей. " * 20
    async with db() as s:
        src = Source(type="media", name="Тульские новости", handle_or_domain="tula.ru")
        s.add(src)
        await s.flush()
        s.add(
            Document(
                url="https://tula.ru/1",
                source_id=src.id,
                title="«Ночь искусств» в Туле",
                text=filler + "Спонсор — Газпром.",
                first_seen_at=NOW - timedelta(minutes=30),
                embedding=_vec(0, 1),
            )
        )
        await s.commit()
    bot = FakeBot()
    stats = await run_topics(db, None, bot, now=NOW)
    assert stats["hits"] == 1 and stats["sent"] == 0  # упоминание вскользь — без оповещения
    async with db() as s:
        hit = await s.scalar(select(TopicHit).where(TopicHit.topic_id == topic_id))
        assert hit.prominent is False and hit.notified is True
