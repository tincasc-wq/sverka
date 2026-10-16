"""Кабинет организации, итерация 2: поиск и фильтры истории, CSV, общие слежения, месячный PDF."""

import json
from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from app.models import Check, Organization, OrgMember, User, Watch, WatchSubscriber
from app.orgs.history import HistoryFilter, category_of, recent_months
from app.watches import run_due_watches, start_watch
from app.web.session import COOKIE, sign
from tests.test_site import client, db  # noqa: F401 — фикстура
from tests.test_watches import FakeBot


def _report(title: str, status: str, index: float, publications: int = 3) -> str:
    return json.dumps(
        {
            "seed": {"url": "https://bbc.com/news/1", "title": title},
            "publications": publications,
            "core_publications": publications,
            "independent_reports": 1,
            "analysis": {"index": index, "status": status, "status_text": status, "claims": []},
        },
        ensure_ascii=False,
    )


async def _org(db):  # noqa: F811
    async with db() as s:
        owner = User(email="boss@ed.test", tg_id=880001)
        staff = User(email="staff@ed.test", tg_id=880002)
        org = Organization(name="Редакция", plan="team", seats=3, monthly_quota=3500)
        s.add_all([owner, staff, org])
        await s.flush()
        s.add_all(
            [
                OrgMember(org_id=org.id, user_id=owner.id, role="owner"),
                OrgMember(org_id=org.id, user_id=staff.id, role="member"),
            ]
        )
        rows = [
            (owner, "Пожар на складе в Подольске", "widely_confirmed", 88.0, 12),
            (staff, "Министр подал в отставку", "disputed", 30.0, 4),
            (staff, "Завод удобрений остановлен", "questioned", 45.0, 6),
        ]
        for i, (user, title, status, index, pubs) in enumerate(rows):
            s.add(
                Check(
                    org_id=org.id,
                    user_id=user.id,
                    input_type="url",
                    input_raw=f"https://example.ru/{i}",
                    status="done",
                    channel="web",
                    public_token=f"tok-h-{i}",
                    summary=_report(title, status, index, pubs),
                    finished_at=datetime.now(UTC),
                )
            )
        s.add(
            Check(
                org_id=org.id,
                user_id=staff.id,
                input_type="url",
                input_raw="https://broken.ru/x",
                status="failed",
                channel="web",
                public_token="tok-h-f",
                summary=json.dumps({"error": "не скачалось"}),
            )
        )
        s.add(  # автоматическая перепроверка — в историю не попадает
            Check(
                org_id=org.id,
                user_id=owner.id,
                input_type="url",
                input_raw="auto",
                status="done",
                channel="auto",
                public_token="tok-h-a",
                summary=_report("Автоперепроверка", "widely_confirmed", 90.0),
            )
        )
        await s.commit()
        return owner, staff, org


def test_filter_parsing_and_categories() -> None:
    f = HistoryFilter.from_params(
        {"q": "  пожар ", "member": "12", "category": "bad", "month": "2026-10"}
    )
    assert (f.q, f.member, f.category, f.month) == ("пожар", 12, "bad", "2026-10")
    junk = HistoryFilter.from_params({"member": "x", "category": "zzz", "month": "2026-13"})
    assert not junk.active
    assert "category=bad" in f.query() and "q=" in f.query()
    assert category_of("done", "refuted") == "bad" and category_of("failed", None) == "failed"
    assert category_of("done", "too_early") == "open" and category_of("done", None) is None
    months = recent_months(14)
    assert len(set(months)) == 14 and months[0] > months[1]


async def test_history_filters_and_csv(db) -> None:  # noqa: F811
    owner, staff, _ = await _org(db)
    async with client() as c:
        c.cookies.set(COOKIE, sign(owner.id))
        everything = await c.get("/org")
        found = await c.get("/org", params={"q": "отставк"})
        bad = await c.get("/org", params={"category": "bad"})
        by_staff = await c.get("/org", params={"member": str(staff.id), "category": "failed"})
        month = await c.get("/org", params={"month": "2001-01"})
        csv = await c.get("/org/export.csv", params={"category": "bad"})

    assert "Пожар на складе" in everything.text and "Автоперепроверка" not in everything.text
    assert "Министр подал" in found.text and "Пожар на складе" not in found.text
    assert "Министр подал" in bad.text and "Завод удобрений" in bad.text
    assert "Пожар на складе" not in bad.text
    assert "broken.ru" in by_staff.text and "Министр" not in by_staff.text
    assert "По этим условиям проверок нет" in month.text
    body = csv.content.decode("utf-8")
    assert body.startswith("﻿Дата (МСК);Сотрудник;")
    assert "Министр подал в отставку" in body and "Пожар" not in body
    assert ";30;" in body and "/r/tok-h-1" in body
    assert 'filename="sverka-history.csv"' in csv.headers["content-disposition"]


async def test_month_report_pdf(db) -> None:  # noqa: F811
    from app.orgs.history import month_summary

    owner, staff, org = await _org(db)
    month = recent_months(1)[0]
    async with db() as s:
        s_org = await s.get(Organization, org.id)
        summary = await month_summary(s, s_org, month, {owner.id: "владелец", staff.id: "сотр"})
    assert summary["total"] == 4 and summary["done"] == 3
    assert summary["by_category"] == {"ok": 1, "open": 0, "bad": 2, "failed": 1}
    assert summary["by_member"][0] == ("сотр", 3) and summary["avg_index"] == 54.3
    assert [r.label for r in summary["top"]][0] == "Пожар на складе в Подольске"
    async with client() as c:
        c.cookies.set(COOKIE, sign(staff.id))
        pdf = await c.get("/org/report.pdf", params={"month": month})
    async with client() as guest:
        anonymous = await guest.get("/org/report.pdf")
    assert pdf.status_code == 200 and pdf.content.startswith(b"%PDF")
    assert anonymous.status_code == 303


async def test_shared_watch_subscribers_get_updates(db, monkeypatch) -> None:  # noqa: F811
    from app.web import checks

    owner, staff, org = await _org(db)
    results = iter([_report("Министр подал в отставку", "disputed", 75.0, 9)])

    async def fake_run(session, check_id, embedder, llm, *, use_cache=True):
        check = await session.get(Check, check_id)
        check.status, check.summary = "done", next(results)
        check.finished_at = datetime.now(UTC)
        await session.commit()
        return check

    monkeypatch.setattr(checks, "run_web_check", fake_run)
    async with db() as s:
        author = await s.get(User, staff.id)
        author.plan = "pro"
        origin = await s.scalar(select(Check).where(Check.public_token == "tok-h-1"))
        watch = await start_watch(s, author, origin, 6, 3)
        watch_id = watch.id
        assert watch.org_id == org.id

    async with client() as c:
        c.cookies.set(COOKIE, sign(owner.id))
        before = await c.get("/org")
        await c.post(f"/watches/{watch_id}/subscribe")
        after = await c.get("/org")
    assert "Слежения команды" in before.text and "Подписаться" in before.text
    assert "Отписаться" in after.text
    async with db() as s:
        assert await s.scalar(select(WatchSubscriber.user_id)) == owner.id
        w = await s.get(Watch, watch_id)
        w.next_run_at = datetime.now(UTC) - timedelta(minutes=1)
        await s.commit()

    bot = FakeBot()
    assert await run_due_watches(db, None, None, bot) == {"changed": 1}
    assert sorted(chat for chat, _ in bot.sent) == [880001, 880002]

    async with client() as c:  # владелец организации может остановить чужое слежение
        c.cookies.set(COOKIE, sign(owner.id))
        await c.post(f"/watches/{watch_id}/unsubscribe")
        await c.post(f"/watches/{watch_id}/stop")
    async with db() as s:
        w = await s.get(Watch, watch_id)
        assert w.active is False and await s.scalar(select(WatchSubscriber)) is None


async def test_outsider_cannot_subscribe(db) -> None:  # noqa: F811
    from app.watches import subscribe

    _, staff, _ = await _org(db)
    async with db() as s:
        author = await s.get(User, staff.id)
        author.plan = "pro"
        origin = await s.scalar(select(Check).where(Check.public_token == "tok-h-1"))
        watch = await start_watch(s, author, origin, 6, 3)
        stranger = User(email="x@else.test")
        s.add(stranger)
        await s.commit()
        assert await subscribe(s, stranger, watch.id, on=True) is False
        assert await subscribe(s, author, watch.id, on=True) is False  # автор и так получает
