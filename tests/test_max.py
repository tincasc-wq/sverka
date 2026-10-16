"""Посты Max: разбор страницы, заглушка вместо поста, шлюз, реестр, неизвестная дата."""

import json
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app import check as check_module
from app.check import run_check
from app.collectors.maxru import MaxStub, channel_handle, parse_post, title_from
from app.core.config import get_settings
from app.models import Check, Document, Source
from app.web.checks import MAX_STUB_REASON, run_web_check
from tests.test_embeddings import FakeEmbedder
from tests.test_site import db  # noqa: F401 — фикстура

FIXTURES = Path(__file__).parent / "fixtures"
POST = (FIXTURES / "max_post.html").read_bytes()
STUB = (FIXTURES / "max_stub.html").read_bytes()
URL = "https://max.ru/RT/AaFBz1MBFGw"


def test_post_page_gives_post_title_and_text_without_date() -> None:
    doc = parse_post(POST, URL, None)
    assert doc.title.startswith("Владимир Путин наградил медалью")  # не «RT на русском»
    assert "Лия Галеева удержала его" in doc.text
    assert "Перейти к посту" not in doc.text and doc.published_at is None


def test_stub_page_is_not_a_post() -> None:
    with pytest.raises(MaxStub):
        parse_post(STUB, URL, None)


def test_channel_handle_and_long_title() -> None:
    assert channel_handle(URL) == "max.ru/rt"
    long = "Очень длинная первая фраза " * 10
    assert len(title_from(long)) <= 140 and title_from(long).endswith("…")


def _fake_fetch(pages: list[bytes], calls: list[dict]):
    async def fetch(client, url):
        calls.append({"url": url, "proxy": getattr(client, "_sverka_proxy", None)})
        return pages[min(len(calls), len(pages)) - 1], url

    return fetch


async def test_stub_is_retried_through_gateway_and_post_joins_registry(
    session: AsyncSession, monkeypatch
) -> None:
    rt_max = Source(
        type="max", name="RT на русском (Max)", handle_or_domain="max.ru/rt", owner="RT"
    )
    session.add(rt_max)
    await session.flush()
    calls: list[dict] = []
    monkeypatch.setattr(check_module, "fetch", _fake_fetch([STUB, POST], calls))
    monkeypatch.setattr(get_settings(), "collector_proxy", "http://gateway:3128")

    result = await run_check(session, URL, FakeEmbedder())
    seed = await session.get(Document, result.document_id)
    report = json.loads(result.summary)

    assert len(calls) == 2  # напрямую — заглушка, через шлюз — пост
    assert seed.source_id == rt_max.id and seed.title.startswith("Владимир Путин")
    [entry] = report["timeline"]
    assert entry["date_unknown"] is True and entry["source"] == "RT на русском (Max)"


async def test_stub_everywhere_fails_with_clear_reason(db, monkeypatch) -> None:  # noqa: F811
    monkeypatch.setattr(check_module, "fetch", _fake_fetch([STUB], []))
    monkeypatch.setattr(get_settings(), "collector_proxy", "http://gateway:3128")
    async with db() as s:
        check = Check(input_type="url", input_raw=URL, status="queued", public_token="max-stub")
        s.add(check)
        await s.commit()
        done = await run_web_check(s, check.id, FakeEmbedder(), None)

    assert done.status == "failed"
    assert json.loads(done.summary)["error"] == MAX_STUB_REASON
