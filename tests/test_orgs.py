"""Кабинет организации: приглашения, места, месячная квота, закрытые отчёты, госконтур."""

import httpx
import pytest
from sqlalchemy import select

from app.bot.service import check_refusal
from app.check import recent_check
from app.llm import LlmClient
from app.llm.config import LlmConfig
from app.models import Check, Organization, OrgMember, User
from app.models.enums import OrgRole
from app.orgs.service import accept_invite, create_invite, membership_of, remove_member
from app.pipeline.llm_steps import ClaimsAnswer
from app.web.auth import COOKIE as ADMIN_COOKIE
from app.web.checks import run_web_check
from tests.test_embeddings import FakeEmbedder
from tests.test_site import _archive, client, db, queued  # noqa: F401 — фикстуры

ADMIN = {ADMIN_COOKIE: "test-admin-token"}


def _token(location: str) -> str:
    return location.rsplit("/", 1)[-1]


async def test_org_from_admin_to_private_member_check(db, queued) -> None:  # noqa: F811
    await _archive(db)
    async with client() as admin:
        admin.cookies.update(ADMIN)
        created = await admin.post(
            "/admin/orgs", data={"name": "Редакция «Тест»", "seats": "2", "monthly_quota": "10"}
        )
        listing = await admin.get(created.headers["location"])
    owner_link = listing.text.split("/join/")[1].split('"')[0]

    async with client() as owner, client() as member, client() as outsider:
        await owner.post(f"/join/{owner_link}")
        invited = await owner.post("/org/invite")
        cabinet = await owner.get(invited.headers["location"])
        member_link = cabinet.text.split("/join/")[1].split('"')[0]
        joined = await member.post(f"/join/{member_link}")
        reused = await outsider.post(f"/join/{member_link}")

        started = await member.post("/check", data={"value": "https://bbc.com/news/1"})
        token = _token(started.headers["location"])
        async with db() as s:
            await run_web_check(s, queued[0][1], FakeEmbedder(), None)

        own_view = await member.get(f"/r/{token}")
        owner_view = await owner.get(f"/r/{token}")
        outsider_view = await outsider.get(f"/r/{token}")
        outsider_wait = await outsider.get(f"/c/{token}")
        org_page = await owner.get("/org")
        member_page = await member.get("/org")

    assert joined.headers["location"] == "/org"
    assert reused.status_code == 409 and "уже использовано" in reused.text
    assert own_view.status_code == 200 and owner_view.status_code == 200
    assert outsider_view.status_code == 404 and outsider_wait.status_code == 404
    assert "Проверок в этом месяце" in org_page.text and "Пожар на складе" in org_page.text
    assert "Расходы на языковые модели" in org_page.text  # владельцу видна себестоимость
    assert "Расходы на языковые модели" not in member_page.text
    assert "Редакция «Тест»" in member_page.text  # ссылка на кабинет в шапке

    async with db() as s:
        check = await s.scalar(select(Check).where(Check.public_token == token))
        org = await s.scalar(select(Organization))
        assert check.org_id == org.id
        # Отчёт организации не отдаётся из кэша посторонним, но её сотрудникам — да.
        assert await recent_check(s, check.document_id, 6) is None
        assert (await recent_check(s, check.document_id, 6, org.id)).id == check.id


async def test_seats_quota_and_suspension(db) -> None:  # noqa: F811
    async with db() as s:
        org = Organization(name="Аппарат", seats=1, monthly_quota=0)
        users = [User(), User()]
        s.add_all([org, *users])
        await s.commit()
        first = await create_invite(s, org, OrgRole.OWNER)
        second = await create_invite(s, org)
        await accept_invite(s, first.token, users[0])
        with pytest.raises(Exception, match="заняты все места"):
            await accept_invite(s, second.token, users[1])

        refusal = await check_refusal(s, users[0])
        assert "Месячная квота организации «Аппарат»" in refusal
        org.monthly_quota = 100
        org.active = False
        await s.commit()
        assert "приостановлен" in await check_refusal(s, users[0])
        org.active = True
        await s.commit()
        assert await check_refusal(s, users[0]) is None

        owner = await s.scalar(select(OrgMember).where(OrgMember.user_id == users[0].id))
        assert not await remove_member(s, org, owner.id)  # владельца не удаляем
        assert (await membership_of(s, users[0])).is_owner


async def test_ru_only_client_never_calls_foreign_models(session) -> None:
    config = LlmConfig.model_validate(
        {
            "providers": {
                "deepseek": {"base_url": "https://api.deepseek.test"},
                "yandex": {"base_url": "https://llm.yandex.test"},
            },
            "routes": {"extract_claims": [{"provider": "deepseek", "model": "deepseek-flash"}]},
            "ru_only_routes": {"extract_claims": [{"provider": "yandex", "model": "alice"}]},
        }
    )
    hosts = []

    def handler(request: httpx.Request) -> httpx.Response:
        hosts.append(request.url.host)
        return httpx.Response(
            200, json={"choices": [{"message": {"content": '{"claims": []}'}}], "usage": {}}
        )

    base = LlmClient(config, httpx.MockTransport(handler), {"deepseek": "k", "yandex": "k"})
    ru = base.restricted(True)
    await ru.complete_json(session, "extract_claims", "s", "u", ClaimsAnswer)
    await base.complete_json(session, "extract_claims", "s", "u", ClaimsAnswer)
    assert hosts == ["llm.yandex.test", "api.deepseek.test"]

    no_yandex = LlmClient(config, api_keys={"deepseek": "k", "yandex": ""}).restricted(True)
    assert not no_yandex.available()  # без российского ключа LLM-шаги просто не выполняются
    assert base.restricted(False) is base
