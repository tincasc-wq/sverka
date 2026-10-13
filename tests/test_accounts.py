"""Одна учётная запись — почта и Telegram: явная привязка и слияние."""

from sqlalchemy import func, select

from app.bot.service import get_or_create_user
from app.core.config import get_settings
from app.models import Check, Organization, OrgMember, User
from app.web.session import COOKIE, sign
from tests.test_email_login import _token, outbox  # noqa: F401 — фикстура
from tests.test_site import _telegram_payload, client, db  # noqa: F401


def _check(user_id: int, token: str) -> Check:
    return Check(
        user_id=user_id, input_type="url", input_raw=token, status="done", public_token=token
    )


async def test_linking_telegram_merges_bot_account_into_email_account(db, monkeypatch) -> None:  # noqa: F811
    monkeypatch.setattr(get_settings(), "telegram_bot_token", "123:abc")
    async with db() as s:
        bot_user = await get_or_create_user(s, 777)  # проверял в боте
        web_user = User(email="me@example.ru", daily_quota=20)
        s.add(web_user)
        await s.flush()
        s.add_all([_check(bot_user.id, "from-bot"), _check(web_user.id, "from-site")])
        await s.commit()

    async with client() as c:
        c.cookies.set(COOKIE, sign(web_user.id))
        linked = await c.get("/auth/telegram/link", params=_telegram_payload("123:abc"))
        account = await c.get(linked.headers["location"])
        history = await c.get("/history")

    async with db() as s:
        users = (await s.scalars(select(User))).all()
        owners = set(await s.scalars(select(Check.user_id)))
        via_bot = await get_or_create_user(s, 777)  # бот находит ту же учётную запись
    assert linked.headers["location"] == "/account?linked=telegram"
    assert len(users) == 1 and users[0].email == "me@example.ru" and users[0].tg_id == 777
    assert owners == {users[0].id} and via_bot.id == users[0].id
    assert "Telegram привязан" in account.text
    assert "from-bot" in history.text and "from-site" in history.text


async def test_linking_email_to_telegram_account(db, outbox) -> None:  # noqa: F811
    async with db() as s:
        tg_user = await get_or_create_user(s, 555)
        await s.commit()
    async with client() as c:
        c.cookies.set(COOKIE, sign(tg_user.id))
        sent = await c.post("/account/email", data={"email": "Tg@Example.ru"})
        token = _token(outbox[0])
        confirm = await c.get(f"/auth/email/{token}")
        done = await c.post(f"/auth/email/{token}")
    async with client() as stranger:  # та же ссылка в чужом браузере не работает
        await stranger.post("/check", data={"value": "https://example.ru/x"})
        foreign = await stranger.post(f"/auth/email/{token}")

    async with db() as s:
        user = await s.get(User, tg_user.id)
    assert "Отправили письмо" in sent.text and outbox[0]["subject"] == "Привязка почты к «Сверке»"
    assert "Привязать <b>tg@example.ru</b>" in confirm.text
    assert done.headers["location"] == "/account?linked=email"
    assert user.email == "tg@example.ru" and user.tg_id == 555
    assert foreign.status_code == 200 and "уже использована" in foreign.text


async def test_link_token_only_for_requesting_account(db, outbox) -> None:  # noqa: F811
    async with db() as s:
        a, b = User(email="a@example.ru"), User(email="b@example.ru")
        s.add_all([a, b])
        await s.commit()
    async with client() as c:
        c.cookies.set(COOKIE, sign(a.id))
        await c.post("/account/email", data={"email": "new@example.ru"})
    # a уже с почтой — привязка второй почты запрещена, но проверим чужой браузер на свежем:
    async with db() as s:
        tg = await get_or_create_user(s, 9001)
        await s.commit()
    async with client() as owner, client() as other:
        owner.cookies.set(COOKIE, sign(tg.id))
        other.cookies.set(COOKIE, sign(b.id))
        await owner.post("/account/email", data={"email": "link@example.ru"})
        refused = await other.post(f"/auth/email/{_token(outbox[-1])}")
    assert "в том же браузере" in refused.text
    async with db() as s:
        assert (await s.get(User, tg.id)).email is None


async def test_merge_refused_across_organizations(db, monkeypatch) -> None:  # noqa: F811
    monkeypatch.setattr(get_settings(), "telegram_bot_token", "123:abc")
    async with db() as s:
        org1, org2 = Organization(name="Один"), Organization(name="Два")
        bot_user = await get_or_create_user(s, 777)
        web_user = User(email="me@example.ru")
        s.add_all([org1, org2, web_user])
        await s.flush()
        s.add_all(
            [
                OrgMember(org_id=org1.id, user_id=web_user.id),
                OrgMember(org_id=org2.id, user_id=bot_user.id),
            ]
        )
        await s.commit()
    async with client() as c:
        c.cookies.set(COOKIE, sign(web_user.id))
        refused = await c.get("/auth/telegram/link", params=_telegram_payload("123:abc"))
    async with db() as s:
        count = await s.scalar(select(func.count()).select_from(User))
    assert refused.headers["location"] == "/account?error=conflict" and count == 2


async def test_plain_email_login_does_not_merge_into_telegram_user(db, outbox) -> None:  # noqa: F811
    async with db() as s:
        tg_user = await get_or_create_user(s, 4242)
        await s.commit()
    async with client() as c:
        c.cookies.set(COOKIE, sign(tg_user.id))
        await c.post("/login/email", data={"email": "other@example.ru"})
        await c.post(f"/auth/email/{_token(outbox[0])}")
    async with db() as s:
        assert (await s.get(User, tg_user.id)).email is None  # не слилось молча
        assert await s.scalar(select(User.id).where(User.email == "other@example.ru")) != tg_user.id
