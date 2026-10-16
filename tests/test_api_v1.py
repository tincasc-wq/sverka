"""API для организаций: ключи из кабинета, постановка проверки, результат, квота, лимит частоты."""

import json
import re
from datetime import UTC, datetime

from sqlalchemy import select

from app.models import ApiKey, Check, Organization, OrgMember, User
from app.orgs.apikeys import hash_key, issue_key
from app.web import api_v1
from app.web.session import COOKIE, sign
from tests.test_site import client, db  # noqa: F401 — фикстура


async def _org(db, quota: int = 100):  # noqa: F811
    async with db() as s:
        owner = User(email="api-owner@ed.test")
        staff = User(email="api-staff@ed.test")
        org = Organization(name="Редакция API", plan="team", seats=3, monthly_quota=quota)
        s.add_all([owner, staff, org])
        await s.flush()
        s.add_all(
            [
                OrgMember(org_id=org.id, user_id=owner.id, role="owner"),
                OrgMember(org_id=org.id, user_id=staff.id, role="member"),
            ]
        )
        await s.commit()
        return owner, staff, org


async def _key(db, owner, org) -> str:  # noqa: F811
    async with db() as s:
        _, raw = await issue_key(s, await s.get(Organization, org.id), owner, "CMS")
        return raw


def _no_rate_limit(monkeypatch, limit: int | None = None) -> None:
    counts: dict[int, int] = {}

    async def fake(key_id: int, kind: str) -> bool:
        counts[key_id] = counts.get(key_id, 0) + 1
        return limit is not None and counts[key_id] > limit

    monkeypatch.setattr(api_v1, "_rate_limited", fake)


def _queue(monkeypatch) -> list[tuple]:
    from app.web import queue

    jobs: list[tuple] = []

    async def fake_enqueue(job: str, *args, **options) -> None:
        jobs.append((job, *args))

    monkeypatch.setattr(queue, "enqueue", fake_enqueue)
    return jobs


async def test_owner_issues_key_shown_once_and_stored_hashed(db) -> None:  # noqa: F811
    owner, staff, _ = await _org(db)
    async with client() as c:
        c.cookies.set(COOKIE, sign(owner.id))
        issued = await c.post("/org/api-keys", data={"name": "CMS редакции"})
        later = await c.get("/org")
    async with client() as c:
        c.cookies.set(COOKIE, sign(staff.id))
        member_page = await c.get("/org")
        refused = await c.post("/org/api-keys", data={"name": "мой"})

    raw = re.search(r'value="(sv_[A-Za-z0-9_-]+)"', issued.text).group(1)
    assert issued.headers["cache-control"] == "no-store"
    assert raw not in later.text and raw[:10] + "…" in later.text and "CMS редакции" in later.text
    assert "Ключи API" not in member_page.text and refused.status_code == 303
    async with db() as s:
        key = await s.scalar(select(ApiKey))
    assert key.key_hash == hash_key(raw) and raw not in key.key_hash


async def test_create_and_fetch_check(db, monkeypatch) -> None:  # noqa: F811
    owner, _, org = await _org(db)
    raw = await _key(db, owner, org)
    jobs = _queue(monkeypatch)
    _no_rate_limit(monkeypatch)
    async with client() as c:
        anonymous = await c.post("/api/v1/checks", json={"input": "https://tass.ru/x/1"})
        wrong = await c.get("/api/v1/usage", headers={"X-API-Key": "sv_nope"})
        created = await c.post(
            "/api/v1/checks",
            json={"input": "https://tass.ru/obschestvo/1"},
            headers={"Authorization": f"Bearer {raw}"},
        )
        junk = await c.post("/api/v1/checks", json={"input": "привет"}, headers={"X-API-Key": raw})
    assert anonymous.status_code == 401 and wrong.status_code == 401
    assert created.status_code == 202 and junk.status_code == 422
    body = created.json()
    assert body["status"] == "queued" and body["report_url"].endswith(f"/r/{body['id']}")
    assert jobs[0][0] == "web_check"

    async with db() as s:
        check = await s.scalar(select(Check).where(Check.public_token == body["id"]))
        assert check.channel == "api" and check.org_id == org.id and check.user_id == owner.id
        check.status = "done"
        check.finished_at = datetime.now(UTC)
        check.summary = json.dumps(
            {
                "seed": {"url": "https://tass.ru/obschestvo/1", "title": "Пожар на складе"},
                "primary": {
                    "source": "ТАСС",
                    "url": "https://tass.ru/obschestvo/1",
                    "time": "2026-10-16T09:00:00+00:00",
                },
                "publications": 9,
                "core_publications": 7,
                "independent_core": 3,
                "analysis": {
                    "index": 81.5,
                    "status": "widely_confirmed",
                    "status_text": "широко подтверждено",
                    "claims": [],
                },
            },
            ensure_ascii=False,
        )
        await s.commit()
    async with client() as c:
        result = await c.get(f"/api/v1/checks/{body['id']}", headers={"X-API-Key": raw})
        usage = await c.get("/api/v1/usage", headers={"X-API-Key": raw})
        missing = await c.get("/api/v1/checks/nope", headers={"X-API-Key": raw})
    r = result.json()
    assert r["status"] == "done" and r["result"]["index"] == 81.5
    assert r["result"]["publications"] == 7 and r["result"]["independent"] == 3
    assert r["result"]["primary"]["source"] == "ТАСС"
    assert usage.json()["month_used"] == 1 and usage.json()["month_left"] == 99
    assert missing.status_code == 404


async def test_quota_revoke_and_departed_owner(db, monkeypatch) -> None:  # noqa: F811
    owner, staff, org = await _org(db, quota=0)
    raw = await _key(db, owner, org)
    _queue(monkeypatch)
    _no_rate_limit(monkeypatch)
    headers = {"X-API-Key": raw}
    async with client() as c:
        over = await c.post("/api/v1/checks", json={"input": "https://tass.ru/1"}, headers=headers)
    assert over.status_code == 402 and "квота" in over.json()["detail"]

    async with db() as s:  # ключ сотрудника, которого удалили, перестаёт работать
        _, staff_raw = await issue_key(s, await s.get(Organization, org.id), staff, "старый")
        member = await s.scalar(select(OrgMember).where(OrgMember.user_id == staff.id))
        await s.delete(member)
        await s.commit()
    async with client() as c:
        gone = await c.get("/api/v1/usage", headers={"X-API-Key": staff_raw})
        c.cookies.set(COOKIE, sign(owner.id))
        key_id = await _first_key_id(db, owner)
        await c.post(f"/org/api-keys/{key_id}/revoke")
        revoked = await c.get("/api/v1/usage", headers=headers)
    assert gone.status_code == 401 and revoked.status_code == 401


async def _first_key_id(db, owner) -> int:  # noqa: F811
    async with db() as s:
        return await s.scalar(select(ApiKey.id).where(ApiKey.user_id == owner.id))


async def test_rate_limit_and_docs(db, monkeypatch) -> None:  # noqa: F811
    owner, _, org = await _org(db)
    raw = await _key(db, owner, org)
    _no_rate_limit(monkeypatch, limit=2)
    async with client() as c:
        codes = [
            (await c.get("/api/v1/usage", headers={"X-API-Key": raw})).status_code for _ in range(3)
        ]
        docs = await c.get("/api/docs")
        schema = (await c.get("/api/openapi.json")).json()
    assert codes == [200, 200, 429]
    assert docs.status_code == 200
    assert set(schema["paths"]) == {"/api/v1/checks", "/api/v1/checks/{check_id}", "/api/v1/usage"}
