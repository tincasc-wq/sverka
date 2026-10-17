"""Тарифы: выбор тарифа, общий лимит бота и сайта, /tariff, назначение в админке."""

from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot import texts
from app.bot.service import check_refusal, get_or_create_user
from app.models import Check, User
from app.plans import daily_limit, get_plans, personal_limit, plan_for, quote, usage_of
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
    pro = User(email="a@b.ru", plan="pro")
    assert personal_limit(pro) == (get_plans()["pro"].monthly_checks, "month")


def test_editorial_constructor_quote() -> None:
    editorial = get_plans()["editorial"]
    c = editorial.constructor
    assert quote(editorial, 0, 0, set()) == (c.base_price, c.base_seats, c.base_checks)
    # 7 мест, 7 500 проверок → округление вверх до шага 1 000 → 8 000
    price, seats, checks = quote(editorial, 7, 7500, set())
    assert (seats, checks) == (7, 8000)
    assert price == c.base_price + 2 * c.seat_price + 2 * c.checks_step_price
    # Мест не больше 15, проверок не больше потолка; российский контур — доплата по запросу,
    # в цену калькулятора не входит (ставка — после замера стоимости российских моделей)
    price_ru, seats, checks = quote(editorial, 99, 10**6, {"ru_only", "bogus"})
    assert seats == c.max_seats == 15 and checks == c.max_checks
    assert price_ru == quote(editorial, 99, 10**6, set())[0]
    assert get_plans()["team"].seats == 3


async def test_pro_limit_is_monthly(session: AsyncSession) -> None:
    user = User(email="monthly@example.ru", plan="pro")
    session.add(user)
    await session.flush()
    old = datetime.now(UTC) - timedelta(days=40)  # прошлый месяц не считается
    session.add_all(
        [Check(user_id=user.id, input_type="url", input_raw="x", status="done", created_at=old)]
        + [
            Check(user_id=user.id, input_type="url", input_raw=str(i), status="done")
            for i in range(3)
        ]
    )
    await session.flush()
    usage = await usage_of(session, user)
    assert usage.period == "month" and usage.used == 3 and usage.limit == 1000
    assert "В этом месяце: 3 из 1000" in texts.format_tariff(usage, get_plans(), "https://x")
    user.daily_quota = 3  # поправка админа — в периоде тарифа
    refusal = await check_refusal(session, user)
    assert refusal is not None and "3 в месяц" in refusal and "в следующем месяце" in refusal


async def test_team_member_sees_team_plan(session: AsyncSession) -> None:
    from app.models import Organization, OrgMember

    org = Organization(name="Тройка", plan="team", seats=3, monthly_quota=2000)
    user = User(email="t@example.ru")
    session.add_all([org, user])
    await session.flush()
    session.add(OrgMember(org_id=org.id, user_id=user.id, role="member"))
    await session.flush()
    usage = await usage_of(session, user)
    assert usage.plan.name == "Команда" and usage.org_limit == 2000


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
    assert "Сегодня: 10 из 10" in reply and "Pro — 1 990 ₽/мес" in reply
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
    assert "Ваш тариф — <b>Pro</b>" in pricing.text and "в этом месяце 0 из 1000" in pricing.text
    assert "Тариф: Pro" in account.text and "действует до" in account.text
    assert "Ваш тариф — <b>Гость</b>" in guest_pricing.text and "Войдите" in guest_pricing.text


async def test_connect_button_sends_request_to_owner(db, monkeypatch) -> None:  # noqa: F811
    from app.models import PlanRequest
    from app.web import notify

    sent: list[str] = []

    async def fake_notify(text: str) -> bool:
        sent.append(text)
        return True

    monkeypatch.setattr(notify, "notify_admin", fake_notify)
    async with db() as s:
        user = User(email="buyer@example.ru")
        s.add(user)
        await s.commit()
    async with client() as c:
        c.cookies.set(COOKIE, sign(user.id))
        done = await c.post(
            "/pricing/request",
            data={"plan": "pro", "comment": "Нужно для редакции блога", "consent": "on"},
        )
        page = await c.get(done.headers["location"])
    async with client() as guest:
        no_contact = await guest.post(
            "/pricing/request", data={"plan": "editorial", "consent": "on"}
        )
        for _ in range(2):  # вместе с заявкой выше — 3 с одного адреса, дальше лимит
            await guest.post(
                "/pricing/request",
                data={
                    "plan": "editorial",
                    "contact": "@someone",
                    "org_name": "Аппарат",
                    "consent": "on",
                },
            )
        limited = await guest.post(
            "/pricing/request", data={"plan": "pro", "contact": "x@y.ru", "consent": "on"}
        )
    async with client() as admin:
        admin.cookies.set("sverka_admin", "test-admin-token")
        listing = await admin.get("/admin/users")
        orgs_page = await admin.get("/admin/orgs")

    async with db() as s:
        items = (await s.scalars(select(PlanRequest).order_by(PlanRequest.id))).all()
    assert done.headers["location"] == "/pricing?requested=pro#request"
    assert "Заявка отправлена" in page.text
    assert "Заявка на тариф «Pro»" in sent[0] and "buyer@example.ru" in sent[0]
    assert "Нужно для редакции блога" in sent[0] and f"/admin/users?q={user.id}" in sent[0]
    assert no_contact.headers["location"] == "/pricing?error=contact#request"
    assert limited.headers["location"] == "/pricing?error=limit#request"
    assert len(items) == 3 and items[1].org_name == "Аппарат" and items[0].notified
    assert "Заявки на тарифы" in listing.text and "buyer@example.ru" in listing.text
    assert 'value="team"' in orgs_page.text and "Команда" in orgs_page.text


async def test_editorial_request_carries_server_quote(db, monkeypatch) -> None:  # noqa: F811
    from app.models import PlanRequest
    from app.web import notify

    sent: list[str] = []

    async def fake_notify(text: str) -> bool:
        sent.append(text)
        return True

    monkeypatch.setattr(notify, "notify_admin", fake_notify)
    async with client() as editor:
        await editor.post(
            "/pricing/request",
            data={
                "plan": "editorial",
                "contact": "chief@news.ru",
                "seats": "7",
                "checks": "7500",
                "options": ["ru_only"],
                "consent": "on",
            },
        )
        page = await editor.get("/pricing")
    async with db() as s:
        item = await s.scalar(select(PlanRequest))
    c = get_plans()["editorial"].constructor
    total = c.base_price + 2 * c.seat_price + 2 * c.checks_step_price
    config = (
        "Конфигурация: 7 мест, 8 000 проверок в месяц, российский контур (доплата по запросу) — "
        + f"{total:,}".replace(",", " ")
        + " ₽/мес"
    )
    assert config in sent[0] and item.comment == config
    assert 'name="seats"' in page.text and "Российский контур" in page.text


async def test_staff_user_enters_admin_without_token(db) -> None:  # noqa: F811
    async with db() as s:
        admin, analyst, plain = (
            User(email="boss@example.ru", role="admin"),
            User(email="an@example.ru", role="analyst"),
            User(email="plain@example.ru"),
        )
        s.add_all([admin, analyst, plain])
        await s.commit()
    async with client() as c:
        c.cookies.set(COOKIE, sign(admin.id))
        home = await c.get("/")
        stats = await c.get("/admin/stats")
        promoted = await c.post(f"/admin/users/{plain.id}/role", data={"role": "analyst"})
    async with client() as c:
        c.cookies.set(COOKIE, sign(analyst.id))
        analyst_stats = await c.get("/admin/stats")
        self_promote = await c.post(f"/admin/users/{analyst.id}/role", data={"role": "admin"})
    async with client() as stranger:
        refused = await stranger.get("/admin/stats")

    assert 'href="/admin/stats">Админка' in home.text
    assert stats.status_code == 200 and "По дням" in stats.text
    assert promoted.status_code == 303 and analyst_stats.status_code == 200
    assert self_promote.status_code == 403
    assert refused.status_code == 303 and refused.headers["location"] == "/admin/login"


async def test_pdf_report_for_paid_plans_only(db) -> None:  # noqa: F811
    import json

    report = {
        "seed": {"url": "https://bbc.com/news/1", "title": "Пожар на складе в Подольске"},
        "primary": {
            "url": "https://bbc.com/news/1",
            "source": "BBC News",
            "time": "2026-10-14T09:00:00+00:00",
        },
        "publications": 1,
        "independent_reports": 1,
        "timeline": [
            {
                "time": "2026-10-14T09:00:00+00:00",
                "source": "BBC News",
                "url": "https://bbc.com/news/1",
                "title": "Пожар на складе в Подольске",
                "role": "primary",
                "membership": "core",
                "took_from": [],
            }
        ],
    }
    async with db() as s:
        pro = User(email="pdf@example.ru", plan="pro")
        free = User(email="free@example.ru")
        s.add_all([pro, free])
        s.add(
            Check(
                input_type="url",
                input_raw="https://bbc.com/news/1",
                status="done",
                public_token="tok-pdf-1",
                summary=json.dumps(report),
                finished_at=datetime.now(UTC),
            )
        )
        await s.commit()
    async with client() as c:
        c.cookies.set(COOKIE, sign(pro.id))
        page = await c.get("/r/tok-pdf-1")
        pdf = await c.get("/r/tok-pdf-1/pdf")
    async with client() as c:
        c.cookies.set(COOKIE, sign(free.id))
        free_page = await c.get("/r/tok-pdf-1")
        refused = await c.get("/r/tok-pdf-1/pdf")

    assert "Скачать PDF" in page.text
    assert pdf.status_code == 200 and pdf.headers["content-type"] == "application/pdf"
    assert pdf.content.startswith(b"%PDF") and len(pdf.content) > 5000
    assert "sverka-tok-pdf-" in pdf.headers["content-disposition"]
    assert "PDF-отчёт — в тарифе Pro" in free_page.text
    assert refused.status_code == 303 and refused.headers["location"] == "/pricing?need=pdf"


async def test_paid_checks_jump_the_queue(db, monkeypatch) -> None:  # noqa: F811
    from app.bot.telegram import check_keyboard
    from app.plans import has_priority, queue_options
    from app.web import site

    calls: list[dict] = []

    async def fake_enqueue(job: str, *args, **options) -> None:
        calls.append(options)

    monkeypatch.setattr(site, "enqueue", fake_enqueue)
    async with db() as s:
        pro, free = User(email="fast@example.ru", plan="pro"), User(email="slow@example.ru")
        s.add_all([pro, free])
        await s.commit()
    for user in (free, pro):
        async with client() as c:
            c.cookies.set(COOKIE, sign(user.id))
            await c.post("/check", data={"value": "https://example.com/news/1"})

    assert calls[0] == {}  # бесплатный — в конец очереди
    head_start = datetime.now(UTC) - calls[1]["_defer_until"]
    assert timedelta(minutes=59) < head_start < timedelta(minutes=61)
    assert has_priority(pro) and not has_priority(free) and queue_options(False) == {}

    def buttons(markup) -> list[str]:
        return [b.callback_data for row in markup.inline_keyboard for b in row]

    assert buttons(check_keyboard(7, pdf=True))[0] == "pdf:7"
    assert "pdf:7" not in buttons(check_keyboard(7))


async def test_admin_note_marks_and_finds_user(db) -> None:  # noqa: F811
    async with db() as s:
        user = User(tg_id=990073)
        s.add(user)
        await s.commit()
    async with client() as admin:
        admin.cookies.set("sverka_admin", "test-admin-token")
        await admin.post(f"/admin/users/{user.id}/note", data={"note": "Тестовый журналист"})
        found = await admin.get("/admin/users", params={"q": "журналист"})
    async with client() as c:
        c.cookies.set(COOKIE, sign(user.id))
        account = await c.get("/account")
    assert "Тестовый журналист" in found.text and "990073" in found.text
    assert "Тестовый журналист" not in account.text


async def test_plan_change_notifies_user_and_shows_banner(db, monkeypatch) -> None:  # noqa: F811
    from app.web import notify

    sent: list[tuple[int, str]] = []

    async def fake_send(chat_id: int, text: str) -> bool:
        sent.append((chat_id, text))
        return True

    monkeypatch.setattr(notify, "send_telegram", fake_send)
    async with db() as s:
        user = User(tg_id=880777)
        s.add(user)
        await s.commit()
    async with client() as admin:
        admin.cookies.set("sverka_admin", "test-admin-token")
        await admin.post(
            f"/admin/users/{user.id}/plan", data={"plan": "pro", "until": "2099-12-31"}
        )
        # то же самое ещё раз — не уведомляем повторно
        await admin.post(
            f"/admin/users/{user.id}/plan", data={"plan": "pro", "until": "2099-12-31"}
        )
    async with client() as c:
        c.cookies.set(COOKIE, sign(user.id))
        page = await c.get("/")
        await c.post("/plan-notice/dismiss")
        later = await c.get("/")

    assert len(sent) == 1 and sent[0][0] == 880777
    assert "Тариф изменён" in sent[0][1] and "«Pro» до 31.12.2099" in sent[0][1]
    assert "(был «Стандарт»)" in sent[0][1] and "PDF-отчёты" in sent[0][1]
    assert "Тариф изменён." in page.text and "1000 проверок в месяц" in page.text
    assert "Тариф изменён." not in later.text


async def test_org_change_notifies_owner(db, monkeypatch) -> None:  # noqa: F811
    from app.models import Organization, OrgMember
    from app.web import notify

    sent: list[str] = []

    async def fake_send(chat_id: int, text: str) -> bool:
        sent.append(text)
        return True

    monkeypatch.setattr(notify, "send_telegram", fake_send)
    async with db() as s:
        owner = User(tg_id=880778)
        org = Organization(name="Тройка", plan="team", seats=3, monthly_quota=3500)
        s.add_all([owner, org])
        await s.flush()
        s.add(OrgMember(org_id=org.id, user_id=owner.id, role="owner"))
        await s.commit()
    async with client() as admin:
        admin.cookies.set("sverka_admin", "test-admin-token")
        await admin.post(
            f"/admin/orgs/{org.id}",
            data={
                "seats": "5",
                "monthly_quota": "6000",
                "active": "on",
                "paid_until": "2099-01-31",
            },
        )
    assert len(sent) == 1
    assert "«Тройка» — «Команда»: 5 мест, 6000 проверок в месяц, оплачено до 31.01.2099" in sent[0]
