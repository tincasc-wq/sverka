"""Повторная проверка по расписанию: кто может следить, когда пишем и как останавливается."""

import json
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Check, User, Watch
from app.watches import WatchRefused, changes, run_due_watches, start_watch, state_of
from app.web.session import COOKIE, sign
from tests.test_site import client, db  # noqa: F401 — фикстура


def _summary(publications: int, independent: int, index: float | None) -> str:
    return json.dumps(
        {
            "seed": {"url": "https://bbc.com/news/1", "title": "Пожар на складе в Подольске"},
            "primary": {
                "url": "https://bbc.com/news/1",
                "source": "BBC News",
                "time": "2026-10-14T09:00:00+00:00",
            },
            "publications": publications,
            "independent_reports": independent,
            "timeline": [],
            "analysis": {
                "index": index,
                "status": "partially_confirmed",
                "status_text": "частично подтверждено",
                "claims": [],
            },
        }
    )


def _check(user: User, summary: str, token: str) -> Check:
    return Check(
        user_id=user.id,
        input_type="url",
        input_raw="https://bbc.com/news/1",
        status="done",
        public_token=token,
        summary=summary,
        finished_at=datetime.now(UTC),
        channel="web",
    )


class FakeBot:
    def __init__(self) -> None:
        self.sent: list[tuple[int, str]] = []

    async def send_message(self, chat_id: int, text: str, **_) -> None:
        self.sent.append((chat_id, text))


def test_changes_ignore_noise() -> None:
    old = {
        "publications": 3,
        "independent": 1,
        "index": 40.0,
        "status_text": "мало",
        "primary": "BBC",
    }
    assert changes(old, dict(old, index=43.0)) == []  # движение индекса меньше шага — шум
    lines = changes(old, dict(old, publications=7, independent=3, index=72.0))
    assert lines[0] == "публикаций о событии: 3 → 7"
    assert "независимых групп источников: 1 → 3" in lines and any("40 → 72" in x for x in lines)


async def test_only_paid_plans_can_watch(session: AsyncSession) -> None:
    free, pro = User(tg_id=501), User(tg_id=502, plan="pro")
    session.add_all([free, pro])
    await session.flush()
    check = _check(pro, _summary(3, 1, 40), "tok-w-1")
    text_only = _check(pro, json.dumps({"matches": [], "days": 7}), "tok-w-2")
    session.add_all([check, text_only])
    await session.flush()

    with pytest.raises(WatchRefused, match="Pro"):
        await start_watch(session, free, check, 6, 3)
    with pytest.raises(WatchRefused, match="расписания"):
        await start_watch(session, pro, check, 5, 3)
    with pytest.raises(WatchRefused, match="завершённой"):
        await start_watch(session, pro, text_only, 6, 3)
    watch = await start_watch(session, pro, check, 12, 7)
    again = await start_watch(session, pro, check, 6, 1)  # повтор — меняет расписание, не плодит
    assert again.id == watch.id and again.every_hours == 6
    assert watch.first_state["publications"] == 3 and watch.first_state["title"].startswith("Пожар")


async def test_watch_reports_changes_then_summary(db, monkeypatch) -> None:  # noqa: F811
    from app.web import checks

    results = iter([_summary(3, 1, 40), _summary(8, 3, 75)])  # 1-й прогон — без изменений

    async def fake_run(session, check_id, embedder, llm, *, use_cache=True):
        check = await session.get(Check, check_id)
        check.status, check.summary = "done", next(results)
        check.finished_at = datetime.now(UTC)
        await session.commit()
        return check

    monkeypatch.setattr(checks, "run_web_check", fake_run)
    async with db() as s:
        user = User(tg_id=777, plan="pro")
        s.add(user)
        await s.flush()
        origin = _check(user, _summary(3, 1, 40), "tok-w-3")
        s.add(origin)
        await s.commit()
        watch = await start_watch(s, user, origin, 6, 1)
        watch_id = watch.id

    bot = FakeBot()

    async def due(**shift) -> dict:
        async with db() as s:
            w = await s.get(Watch, watch_id)
            w.next_run_at = datetime.now(UTC) - timedelta(minutes=1)
            if shift:
                w.until = datetime.now(UTC) - timedelta(minutes=1)
            await s.commit()
        return await run_due_watches(db, None, None, bot)

    assert await due() == {"same": 1} and bot.sent == []
    assert await due() == {"changed": 1}
    chat, text = bot.sent[0]
    assert chat == 777 and "Обновление" in text and "публикаций о событии: 3 → 8" in text
    assert "/r/" in text
    assert await due(expired=True) == {"finished": 1}
    final = bot.sent[1][1]
    assert "Слежение закончено" in final and "Повторных проверок: 2" in final and "3 → 8" in final
    async with db() as s:
        w = await s.get(Watch, watch_id)
        repeats = [c for c in (await s.get(Check, w.last_check_id),) if c.channel == "watch"]
    assert not w.active and w.stopped_reason == "срок истёк" and repeats


async def test_site_watch_form_and_stop(db) -> None:  # noqa: F811
    async with db() as s:
        pro, free = User(email="w@example.ru", plan="pro"), User(email="nw@example.ru")
        s.add_all([pro, free])
        await s.flush()
        s.add(_check(pro, _summary(2, 1, 30), "tok-w-4"))
        await s.commit()
    async with client() as c:
        c.cookies.set(COOKIE, sign(pro.id))
        before = await c.get("/r/tok-w-4")
        await c.post("/r/tok-w-4/watch", data={"every": "12", "days": "3"})
        after = await c.get("/r/tok-w-4")
        account = await c.get("/account")
    async with db() as s:
        from sqlalchemy import select

        watch = await s.scalar(select(Watch).where(Watch.user_id == pro.id))
    async with client() as c:
        c.cookies.set(COOKIE, sign(pro.id))
        await c.post(f"/watches/{watch.id}/stop")
        stopped = await c.get("/r/tok-w-4")
    async with client() as c:
        c.cookies.set(COOKIE, sign(free.id))
        refused = await c.post("/r/tok-w-4/watch", data={"every": "6", "days": "1"})

    assert "Следить за новостью" in before.text and 'name="every"' in before.text
    assert "Слежу за этой новостью" in after.text and "каждые 12 ч" in after.text
    assert "Слежу за новостями" in account.text and "Пожар на складе" in account.text
    assert "Остановить слежение" not in stopped.text and 'name="every"' in stopped.text
    assert "watch_error=" in refused.headers["location"]
    assert state_of(_check(pro, _summary(2, 1, None), "x"))["index"] is None
