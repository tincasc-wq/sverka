"""«Проверить через час»: по кнопке, для платных тарифов; результат — сообщением и ссылкой."""

import json
from datetime import UTC, datetime

from app.models import Check, User
from app.web.session import COOKIE, sign
from tests.test_site import client, db  # noqa: F401 — фикстура
from tests.test_watches import FakeBot, _check, _summary


async def test_recheck_button_schedules_and_reports_result(db, monkeypatch) -> None:  # noqa: F811
    from app import rechecks
    from app.web import checks, queue

    jobs: list[tuple] = []

    async def fake_enqueue(job: str, *args, **options) -> None:
        jobs.append((job, *args, options))

    async def fake_run(session, check_id, embedder, llm, *, use_cache=True):
        check = await session.get(Check, check_id)
        check.status, check.summary = "done", _summary(9, 3, 80)
        check.finished_at = datetime.now(UTC)
        await session.commit()
        return check

    monkeypatch.setattr(queue, "enqueue", fake_enqueue)
    monkeypatch.setattr(checks, "run_web_check", fake_run)
    async with db() as s:
        pro, free = User(tg_id=990101, plan="pro"), User(email="rc-free@site.test")
        s.add_all([pro, free])
        await s.flush()
        s.add_all(
            [
                _check(pro, _summary(1, 0, None), "tok-rc-1"),
                _check(free, _summary(1, 0, None), "tok-rc-2"),
            ]
        )
        await s.commit()

    async with client() as c:
        c.cookies.set(COOKIE, sign(free.id))
        free_page = await c.get("/r/tok-rc-2")
        refused = await c.post("/r/tok-rc-2/recheck")
    async with client() as c:
        c.cookies.set(COOKIE, sign(pro.id))
        before = await c.get("/r/tok-rc-1")
        await c.post("/r/tok-rc-1/recheck")
        twice = await c.post("/r/tok-rc-1/recheck")
        pending = await c.get("/r/tok-rc-1")

    assert "Проверка через час с уведомлением" in free_page.text
    assert "recheck_error" in refused.headers["location"]
    assert "⏰ Проверить через час" in before.text and "Проверю ещё раз в" in pending.text
    assert "recheck_error" in twice.headers["location"] and len(jobs) == 1
    job, check_id, options = jobs[0]
    assert job == "hour_recheck" and options["_defer_by"].total_seconds() == 3600

    bot = FakeBot()
    async with db() as s:
        assert await rechecks.run_recheck(s, check_id, None, None, bot) == "partially_confirmed"
    async with client() as c:
        c.cookies.set(COOKIE, sign(pro.id))
        done = await c.get("/r/tok-rc-1")
    chat, text = bot.sent[0]
    assert chat == 990101 and "Проверка через час" in text and "Сейчас: 80" in text
    assert "Новый отчёт →" in done.text
    async with db() as s:
        origin = await s.get(Check, check_id)
        assert json.loads(origin.summary)["recheck"]["token"]


def test_bot_button_is_an_hour_for_paid_plans() -> None:
    from app.bot.telegram import check_keyboard

    paid = check_keyboard(5, pdf=True, watch=True).inline_keyboard
    free = check_keyboard(5).inline_keyboard
    labels = [b.text for row in paid for b in row]
    assert "🔁 Проверить через час" in labels
    assert any(b.callback_data == "recheck:5:1" for row in paid for b in row)
    assert any(b.callback_data == "recheck:5:6" for row in free for b in row)
