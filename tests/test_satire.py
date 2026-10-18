"""Сатирические издания: шутка-первоисточник, проверка самой сатиры, статистика админки."""

import json

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot import texts
from app.check import run_check
from app.models import Source
from tests.test_embeddings import FakeEmbedder
from tests.test_llm import client as llm_client
from tests.test_site import client, db, queued  # noqa: F401 — фикстуры
from tests.test_story import _doc


async def _sources(session: AsyncSession):
    satire = Source(
        type="satire", name="ИА «Панорама»", handle_or_domain="panorama.pub", owner="ИА «Панорама»"
    )
    media = Source(type="media", name="Газета", handle_or_domain="gazeta.test")
    other = Source(type="agency", name="Агентство", handle_or_domain="agency.test")
    session.add_all([satire, media, other])
    await session.flush()
    return satire, media, other


def _no_llm_calls(request: httpx.Request) -> httpx.Response:
    raise AssertionError("сатиру не сверяем через LLM")


async def test_satire_origin_marks_story_as_joke(session: AsyncSession) -> None:
    satire, media, other = await _sources(session)
    await _doc(session, "https://panorama.pub/news/1", 0.95, 0, satire, "Шуточная новость")
    seed = await _doc(session, "https://gazeta.test/1", 1.0, 60, media, "Перепечатка шутки")
    await _doc(session, "https://agency.test/1", 0.94, 90, other, "Ещё перепечатка")

    check = await run_check(
        session, f"doc:{seed.id}", FakeEmbedder(), llm=llm_client(_no_llm_calls)
    )
    report = json.loads(check.summary)

    assert report["satire"]["origin"] and "сатирическое издание" in report["satire"]["source"]
    assert report["analysis"]["status"] == "satire_origin" and report["analysis"]["index"] is None
    assert check.confirmation_index is None
    text = texts.format_check(report, None, "http://x/r/1")
    assert "Похоже на шутку" in text and "Индекс подтверждённости" not in text


async def test_checking_satire_itself_and_satire_never_confirms(session: AsyncSession) -> None:
    satire, media, _ = await _sources(session)
    real = await _doc(session, "https://gazeta.test/real", 0.95, 0, media, "Настоящая новость")
    joke = await _doc(session, "https://panorama.pub/news/2", 1.0, 60, satire, "Шутка по мотивам")

    check = await run_check(session, f"doc:{joke.id}", FakeEmbedder())
    report = json.loads(check.summary)
    assert report["satire"] == {**report["satire"], "origin": False, "seed": True}
    assert report["analysis"]["status"] == "satire_seed"
    assert "Это сатирическое издание" in texts.format_check(report, None, "http://x/r/1")

    other = await run_check(session, f"doc:{real.id}", FakeEmbedder())
    real_report = json.loads(other.summary)
    assert real_report["satire"] is None
    assert real_report["independent_reports"] == 1  # сатира не засчитана как подтверждение


async def test_admin_stats_page(db, queued) -> None:  # noqa: F811
    async with client() as c:
        c.cookies.set("sverka_admin", "test-admin-token")
        await c.post("/check", data={"value": "https://example.ru/news/1"})
        page = await c.get("/admin/stats?days=7")
    assert page.status_code == 200
    assert "По дням" in page.text and "Пользователей всего" in page.text


def test_parody_channel_description_and_lookalike_name() -> None:
    from app.collectors.channel_profile import (
        ChannelProfile,
        imitated,
        parody_marker,
        parse_profile,
    )
    from app.pipeline.graph import SourceInfo

    html = (
        '<div class="tgme_page_title"><span dir="auto">Раньше всех? Ну почти.</span></div>'
        '<div class="tgme_page_description">Все что написано здесь неправда, вас '
        "наебунькали. Ну почти. Пародийный канал.</div>"
    )
    profile = parse_profile("vas_naebali_news", html)
    assert profile.title == "Раньше всех? Ну почти."
    assert "Пародийный канал" in parody_marker(profile)
    real = SourceInfo(
        9, "Раньше всех. Ну почти", "@bbbreaking", "aggregator", aliases=("Раньше всех",)
    )
    assert imitated(profile, [real]) == "Раньше всех. Ну почти (@bbbreaking)"
    assert imitated(ChannelProfile("bbbreaking", "Раньше всех. Ну почти", ""), [real]) is None

    news = ChannelProfile("rian_ru", "РИА Новости", "Главные новости России и мира. Оперативно.")
    assert parody_marker(news) is None
    assert parody_marker(ChannelProfile("x", "Fake Daily", "Satirical news, nothing here is real."))
    assert parody_marker(ChannelProfile("y", "Новости", "Всё, что мы публикуем, — вымысел."))


async def test_unregistered_parody_channel_becomes_satire(session, monkeypatch) -> None:
    from app.collectors import channel_profile as cp

    async def fake_profile(handle: str):
        return cp.ChannelProfile(handle, "Раньше всех? Ну почти.", "Пародийный канал.")

    monkeypatch.setattr(cp, "channel_profile", fake_profile)
    seed = await _doc(
        session, "https://t.me/vas_naebali_news/353", 1.0, text="Трамп отправит «Патриоты»"
    )
    check = await run_check(
        session, f"doc:{seed.id}", FakeEmbedder(), llm=llm_client(_no_llm_calls)
    )
    report = json.loads(check.summary)
    assert report["satire"]["seed"] and "Пародийный канал" in report["satire"]["reason"]
    assert report["analysis"]["status"] == "satire_seed"
