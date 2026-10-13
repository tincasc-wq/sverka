"""Вход по почте одноразовой ссылкой."""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from app.models import Check, LoginToken, Organization, OrgMember, User
from app.models.enums import OrgRole
from app.orgs.service import create_invite
from app.web import email_auth
from tests.test_site import client, db, queued  # noqa: F401 — фикстуры


@pytest.fixture
def outbox(monkeypatch) -> list[dict]:
    sent: list[dict] = []

    async def fake_send(to, subject, text, html=None) -> bool:
        sent.append({"to": to, "subject": subject, "text": text})
        return True

    monkeypatch.setattr(email_auth, "send_mail", fake_send)
    return sent


def _token(mail: dict) -> str:
    link = next(w for w in mail["text"].split() if "/auth/email/" in w)
    return link.rsplit("/", 1)[-1]


async def test_email_login_creates_user_and_keeps_browser_history(db, queued, outbox) -> None:  # noqa: F811
    async with client() as c:
        await c.post("/check", data={"value": "https://bbc.com/news/1"})
        sent = await c.post("/login/email", data={"email": "  Reader@Example.RU "})
        token = _token(outbox[0])
        confirm = await c.get(f"/auth/email/{token}")
        again_page = await c.get(f"/auth/email/{token}")  # почтовый сканер: GET ничего не тратит
        logged = await c.post(f"/auth/email/{token}")
        history = await c.get("/history")
        reused = await c.post(f"/auth/email/{token}")

    assert "Проверьте почту" in sent.text and outbox[0]["to"] == "reader@example.ru"
    assert "Войти как <b>reader@example.ru</b>" in confirm.text and "Войти" in again_page.text
    assert logged.headers["location"] == "/history"
    assert "reader@example.ru" in history.text and "bbc.com/news/1" in history.text
    assert "уже использована" in reused.text

    async with db() as s:
        user = await s.scalar(select(User).where(User.email == "reader@example.ru"))
        owners = set(await s.scalars(select(Check.user_id)))
        stored = (await s.scalars(select(LoginToken.token_hash))).all()
    assert user.email_verified_at is not None and user.daily_quota == 20
    assert owners == {user.id}  # проверка из браузера перешла к пользователю
    assert token not in stored  # в базе только хэш


async def test_existing_email_logs_into_same_account(db, outbox) -> None:  # noqa: F811
    async with db() as s:
        s.add(User(email="owner@example.ru", daily_quota=20))
        await s.commit()
    async with client() as c:
        await c.post("/login/email", data={"email": "owner@example.ru"})
        await c.post(f"/auth/email/{_token(outbox[0])}")
    async with db() as s:
        users = (await s.scalars(select(User).where(User.email == "owner@example.ru"))).all()
    assert len(users) == 1


async def test_invalid_rate_limited_and_expired(db, outbox) -> None:  # noqa: F811
    async with client() as c:
        bad = await c.post("/login/email", data={"email": "not-an-email"})
        for _ in range(3):
            await c.post("/login/email", data={"email": "spam@example.ru"})
        limited = await c.post("/login/email", data={"email": "spam@example.ru"})
    assert bad.status_code == 422 and "опечатка" in bad.text
    assert limited.status_code == 422 and len(outbox) == 3

    async with db() as s:
        item = await s.scalar(select(LoginToken).limit(1))
        item.expires_at = datetime.now(UTC) - timedelta(minutes=1)
        await s.commit()
    async with client() as c:
        expired = await c.post(f"/auth/email/{_token(outbox[0])}")
    assert "Срок ссылки истёк" in expired.text


async def test_org_membership_follows_login(db, outbox) -> None:  # noqa: F811
    async with db() as s:
        org = Organization(name="Редакция", seats=3)
        s.add(org)
        await s.commit()
        invite = await create_invite(s, org, OrgRole.MEMBER)
    async with client() as c:
        await c.post(f"/join/{invite.token}")  # вступил до входа — из браузера
        await c.post("/login/email", data={"email": "member@example.ru"})
        await c.post(f"/auth/email/{_token(outbox[0])}")
        cabinet = await c.get("/org")
    async with db() as s:
        user = await s.scalar(select(User).where(User.email == "member@example.ru"))
        member = await s.scalar(select(OrgMember).where(OrgMember.org_id == org.id))
    assert member.user_id == user.id
    assert "Редакция" in cabinet.text and "Проверок в этом месяце" in cabinet.text
