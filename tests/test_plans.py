"""Тарифы: выбор тарифа, общий лимит бота и сайта, /tariff, назначение в админке."""

from datetime import UTC, datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from app.bot import texts
from app.bot.service import check_refusal, get_or_create_user
from app.models import Check, User
from app.plans import daily_limit, get_plans, plan_for, usage_of
from app.web.session import COOKIE, sign
from tests.test_site import client, db  # noqa: F401 — фикстура


def test_plan_selection() -> None:
    now = datetime.now(UTC)
    assert plan_for(None).code == "guest" and plan_for(User()).code == "guest"
    assert plan_for(User(tg_id=1)).code == "standard"  # пользователь бота — уже «Стандарт»
    assert (
        plan_for(User(email="a@b.ru", plan="pro", plan_until=now + timedelta(days=3))).code == "pro"
    )
    assert (
        plan_for(User(email="a@b.ru", plan="pro", plan_until=now - timedelta(days=1))).code
        == "standard"
    )
    assert plan_for(User(email="a@b.ru", plan="pro")).code == "pro"  # без срока — бессрочно
    assert daily_limit(User(tg_id=1)) == get_plans()["standard"].daily_checks
    assert daily_limit(User(tg_id=1, daily_quota=3)) == 3  # личная поправка админа


async def test_bot_and_site_share_one_daily_limit(session: AsyncSession) -> None:
    user = await get_or_create_user(session, 31337)  # только бот, на сайт не заходил
    limit = daily_limit(user)
    for i in range(limit):
        channel = "bot" if i % 2 else "web"
        session.add(
            Check(
                user_id=user.id, input_type="url", input_raw=str(i), status="done", channel=channel
            )
        )
    await session.flush()

    refusal = await check_refusal(session, user)
    usage = await usage_of(session, user)
    reply = texts.format_tariff(usage, get_plans(), "https://ang-rist.ru")

    assert refusal is not None and "«Стандарт»" in refusal and "/tariff" in refusal
    assert usage.used == limit and usage.left == 0
    assert "Сегодня: 10 из 10" in reply and "Pro — 690 ₽/мес" in reply
    assert "https://ang-rist.ru/pricing" in reply


async def test_admin_assigns_plan_and_pricing_shows_it(db) -> None:  # noqa: F811
    async with db() as s:
        user = User(email="pro@example.ru")
        s.add(user)
        await s.commit()
    async with client() as admin:
        admin.cookies.set("sverka_admin", "test-admin-token")
        saved = await admin.post(
            f"/admin/users/{user.id}/plan", data={"plan": "pro", "until": "2099-12-31"}
        )
        listing = await admin.get(saved.headers["location"])
    async with client() as c:
        c.cookies.set(COOKIE, sign(user.id))
        pricing = await c.get("/pricing")
        account = await c.get("/account")
    async with client() as guest:
        guest_pricing = await guest.get("/pricing")

    assert "Сохранено" in listing.text and "Pro" in listing.text
    assert "Ваш тариф — <b>Pro</b>" in pricing.text and "0 из 100" in pricing.text
    assert "Тариф: Pro" in account.text and "действует до" in account.text
    assert "Ваш тариф — <b>Гость</b>" in guest_pricing.text and "Войдите" in guest_pricing.text
