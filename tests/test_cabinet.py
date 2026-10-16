"""Плашка тарифа в шапке, кабинет организации: срок оплаты, смена тарифа, оповещения о квоте."""

from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from app.models import Check, Organization, OrgMember, PlanRequest, User
from app.orgs.alerts import run_quota_alerts, threshold_reached
from app.web.session import COOKIE, sign
from tests.test_site import client, db  # noqa: F401 — фикстура


async def _team(db, quota: int = 3500, paid_until=None):  # noqa: F811
    async with db() as s:
        owner = User(email="owner@team.test", tg_id=777001)
        org = Organization(
            name="Тройка", plan="team", seats=3, monthly_quota=quota, paid_until=paid_until
        )
        s.add_all([owner, org])
        await s.flush()
        s.add(OrgMember(org_id=org.id, user_id=owner.id, role="owner"))
        await s.commit()
        return owner, org


async def test_header_shows_plan_and_usage(db) -> None:  # noqa: F811
    async with db() as s:
        plain = User(email="plain@site.test")
        s.add(plain)
        await s.commit()
    owner, _ = await _team(db)
    async with client() as c:
        c.cookies.set(COOKIE, sign(plain.id))
        personal = await c.get("/")
    async with client() as c:
        c.cookies.set(COOKIE, sign(owner.id))
        member = await c.get("/methodology")
    async with client() as guest:
        anonymous = await guest.get("/")

    assert 'class="plan-badge' in personal.text and "Стандарт · 0 / 10" in personal.text
    assert 'href="/org" class="plan-badge' in member.text and "Команда · 0 / 3500" in member.text
    assert "Гость · 0 / 5" in anonymous.text


async def test_cabinet_shows_plan_name_and_payment_term(db) -> None:  # noqa: F811
    soon = datetime.now(UTC) + timedelta(days=3)
    owner, _ = await _team(db, paid_until=soon)
    async with client() as c:
        c.cookies.set(COOKIE, sign(owner.id))
        page = await c.get("/org")

    assert "Тариф «Команда»" in page.text and "оплачено до" in page.text
    assert "Оплата заканчивается" in page.text and "Изменить тариф" in page.text


async def test_owner_requests_plan_change(db, monkeypatch) -> None:  # noqa: F811
    from app.web import notify

    sent: list[str] = []

    async def fake_notify(text: str) -> bool:
        sent.append(text)
        return True

    monkeypatch.setattr(notify, "notify_admin", fake_notify)
    owner, _ = await _team(db)
    async with client() as c:
        c.cookies.set(COOKIE, sign(owner.id))
        done = await c.post(
            "/org/change-plan",
            data={"plan": "editorial", "seats": "7", "checks": "8000", "comment": "с 1 ноября"},
        )
        page = await c.get(done.headers["location"])
    async with db() as s:
        item = await s.scalar(select(PlanRequest))

    assert done.headers["location"] == "/org?requested=1#plan"
    assert "Запрос на изменение тарифа отправлен" in page.text
    assert "«Тройка» просит изменить тариф" in sent[0] and "7 мест, 8000 проверок" in sent[0]
    assert "₽/мес" in sent[0] and "с 1 ноября" in sent[0]
    assert item.org_name == "Тройка" and item.plan == "editorial"


async def test_quota_alerts_once_per_threshold(db) -> None:  # noqa: F811
    owner, org = await _team(db, quota=10)

    class Redis:
        keys: set[str] = set()

        async def set(self, key, value, ex=None, nx=False):
            if nx and key in self.keys:
                return None
            self.keys.add(key)
            return True

    class Bot:
        sent: list[tuple[int, str]] = []

        async def send_message(self, chat_id, text, **_):
            self.sent.append((chat_id, text))

    async def add_checks(n: int) -> None:
        async with db() as s:
            s.add_all(
                Check(
                    org_id=org.id, user_id=owner.id, input_type="url", input_raw="x", status="done"
                )
                for _ in range(n)
            )
            await s.commit()

    redis, bot = Redis(), Bot()
    await add_checks(8)
    assert await run_quota_alerts(db, redis, bot) == 1
    assert await run_quota_alerts(db, redis, bot) == 0  # в этом месяце уже сообщали
    await add_checks(2)
    assert await run_quota_alerts(db, redis, bot) == 1
    assert "80% квоты" in bot.sent[0][1] and "израсходовала месячную квоту" in bot.sent[1][1]
    assert bot.sent[0][0] == 777001
    assert threshold_reached(5, 10) is None and threshold_reached(0, 0) is None
