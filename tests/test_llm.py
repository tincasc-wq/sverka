"""LLM-часть этапа 3 без сети: адаптер, проверка цитат, индекс, сквозная проверка."""

import json
from decimal import Decimal

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.check import run_check
from app.core.scoring import IndexConfig
from app.llm import LlmClient, LlmUnavailable
from app.llm.config import LlmConfig
from app.models import Claim, ClaimEvidence, LlmCall, Source
from app.pipeline.index import Evidence, score_claim, score_news
from app.pipeline.llm_steps import ClaimsAnswer, quote_found, verify_document
from tests.test_embeddings import FakeEmbedder
from tests.test_story import _doc

CONFIG = LlmConfig.model_validate(
    {
        "providers": {
            "deepseek": {"base_url": "https://api.deepseek.test"},
            "qwen": {"base_url": "https://qwen.test"},
            "anthropic": {"enabled": False},
        },
        "routes": {
            step: [
                {"provider": "deepseek", "model": "deepseek-flash", "thinking": "disabled"},
                {"provider": "qwen", "model": "qwen-flash"},
                {"provider": "anthropic", "model": "claude"},
            ]
            for step in ("extract_claims", "same_event", "verify")
        }
        | {"verify_disputed": [{"provider": "deepseek", "model": "deepseek-v4-pro"}]},
        "daily_budget_usd": {"deepseek": 1},
        "prices": {"deepseek-flash": {"input": 1, "output": 1}},
    }
)
KEYS = {"deepseek": "k1", "qwen": "k2", "anthropic": "k3"}


def answer(content: dict | str, tokens: int = 100) -> httpx.Response:
    body = content if isinstance(content, str) else json.dumps(content, ensure_ascii=False)
    return httpx.Response(
        200,
        json={
            "choices": [{"message": {"content": body}}],
            "usage": {"prompt_tokens": tokens, "completion_tokens": tokens},
        },
    )


def client(handler) -> LlmClient:
    return LlmClient(CONFIG, httpx.MockTransport(handler), KEYS)


CLAIMS = {"claims": [{"text": "В Подольске горит склад площадью 2000 кв. м"}]}


# ---- адаптер ---------------------------------------------------------------------------


async def test_falls_back_to_next_provider_and_logs_reason(session: AsyncSession) -> None:
    hosts = []

    def handler(request: httpx.Request) -> httpx.Response:
        hosts.append(request.url.host)
        if request.url.host == "api.deepseek.test":
            return httpx.Response(503)
        assert request.headers["authorization"] == "Bearer k2"
        return answer(CLAIMS)

    result = await client(handler).complete_json(session, "extract_claims", "s", "u", ClaimsAnswer)

    assert result.claims[0].text.startswith("В Подольске")
    assert hosts == ["api.deepseek.test", "qwen.test"]
    calls = (await session.scalars(select(LlmCall).order_by(LlmCall.id))).all()
    assert [c.provider for c in calls] == ["deepseek", "qwen"]
    assert "503" in calls[0].fallback_reason
    assert calls[1].fallback_reason.startswith("deepseek/")


async def test_invalid_json_is_retried_once_on_same_model(session: AsyncSession) -> None:
    replies = iter([answer("не json"), answer(CLAIMS)])

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        assert payload["thinking"] == {"type": "disabled"}
        assert payload["response_format"] == {"type": "json_object"}
        return next(replies)

    await client(handler).complete_json(session, "extract_claims", "s", "u", ClaimsAnswer)

    calls = (await session.scalars(select(LlmCall).order_by(LlmCall.id))).all()
    assert [c.provider for c in calls] == ["deepseek", "deepseek"]
    assert "invalid JSON" in calls[0].fallback_reason
    assert calls[1].cost_usd == Decimal("0.0002")  # 200 токенов по $1 за миллион


async def test_exhausted_daily_budget_skips_provider(session: AsyncSession) -> None:
    session.add(
        LlmCall(stage="verify", provider="deepseek", model="deepseek-flash", cost_usd=Decimal(2))
    )
    await session.flush()

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "qwen.test"
        return answer(CLAIMS)

    await client(handler).complete_json(session, "extract_claims", "s", "u", ClaimsAnswer)


async def test_no_working_route_raises(session: AsyncSession) -> None:
    llm = client(lambda request: httpx.Response(500))
    with pytest.raises(LlmUnavailable):
        await llm.complete_json(session, "extract_claims", "s", "u", ClaimsAnswer)

    no_keys = LlmClient(CONFIG, api_keys={"deepseek": "", "qwen": "", "anthropic": "k"})
    assert not no_keys.available()  # у Claude ключ есть, но провайдер выключен


# ---- цитаты ----------------------------------------------------------------------------


def test_quote_must_be_verbatim() -> None:
    text = "Пожар на складе. «Площадь  возгорания — 2000 квадратных метров», сообщили в МЧС."
    assert quote_found('площадь возгорания — 2000 квадратных метров"', text)
    assert not quote_found("площадь возгорания 3000 квадратных метров", text)
    assert not quote_found("2000", text)  # слишком коротко, чтобы что-то доказывать


async def test_verdict_without_real_quote_is_rejected(session: AsyncSession) -> None:
    doc = await _doc(session, "https://a.ru/1", 1.0, text="Склад в Подольске потушили к вечеру.")

    def handler(request: httpx.Request) -> httpx.Response:
        return answer(
            {
                "results": [
                    {"claim": 1, "stance": "supports", "quote": "склад площадью 2000 кв. м горит"},
                    {"claim": 2, "stance": "supports", "quote": "Склад в Подольске потушили"},
                    {"claim": 9, "stance": "supports", "quote": "лишний номер"},
                ]
            }
        )

    results, rejected = await verify_document(client(handler), session, ["a", "b"], doc)

    assert [(r.claim, r.stance) for r in results] == [(1, "unrelated"), (2, "supports")]
    assert rejected == 1


# ---- индекс ----------------------------------------------------------------------------

CFG = IndexConfig()


def ev(group: str, stance: str, independent: bool = True, kind: str = "media") -> Evidence:
    return Evidence(group, kind, 50, stance, independent)


def test_reprints_and_unrelated_do_not_move_index() -> None:
    base = score_claim([], 50, CFG)
    noise = score_claim([ev("a", "supports", False), ev("b", "unrelated")], 50, CFG)
    assert base.index == noise.index == 50


def test_one_group_one_vote_and_contradiction_wins() -> None:
    twice = score_claim([ev("a", "supports"), ev("a", "supports")], 50, CFG)
    once = score_claim([ev("a", "supports")], 50, CFG)
    assert twice.index == once.index > 50

    mixed = score_claim([ev("a", "supports"), ev("a", "contradicts")], 50, CFG)
    assert mixed.independent_groups == {"a": "contradicts"} and mixed.index < 50


def test_more_independent_and_official_sources_raise_index() -> None:
    two = score_claim([ev("a", "supports"), ev("b", "supports")], 50, CFG)
    official = score_claim([ev("a", "supports", kind="official")], 50, CFG)
    tg = score_claim([ev("a", "supports", kind="tg")], 50, CFG)
    assert two.index > score_claim([ev("a", "supports")], 50, CFG).index
    assert official.index > tg.index


def test_news_index_statuses() -> None:
    assert score_news([[]], [False], 50, 1, CFG).status == "no_checkable_claims"
    assert score_news([[]], [True], 50, 1, CFG).status == "too_early"
    late = score_news([[]], [True], 50, 48, CFG)
    assert late.index is None and late.status == "no_independent"
    trusted = score_claim([], 90, CFG)
    assert trusted.index > 75 and trusted.status == "not_confirmed"
    # Главное утверждение весит вдвое: (75·2 + 50) / 3
    weighted = score_news([[ev("a", "supports")], []], [True, True], 50, 48, CFG)
    first = weighted.claims[0].index
    assert weighted.index == round((first * 2 + 50) / 3, 1)


# ---- сквозная проверка -----------------------------------------------------------------


def fake_model(request: httpx.Request) -> httpx.Response:
    """Отвечает как модель на каждый шаг конвейера по содержимому запроса."""
    payload = json.loads(request.content)
    user = payload["messages"][1]["content"]
    if "Кандидаты:" in user:
        return answer({"decisions": []})
    if "<claims>" not in user:
        return answer(CLAIMS)
    if "3000" in user:
        return answer(
            {
                "results": [
                    {
                        "claim": 1,
                        "stance": "contradicts",
                        "quote": "площадь пожара 3000 квадратных метров",
                        "discrepancy": "3000 кв. м вместо 2000",
                    }
                ]
            }
        )
    return answer(
        {"results": [{"claim": 1, "stance": "supports", "quote": "склад площадью 2000 кв. м"}]}
    )


async def test_check_with_llm_builds_claims_evidence_and_index(session: AsyncSession) -> None:
    tass = Source(type="agency", name="ТАСС", handle_or_domain="tass.ru")
    ifx = Source(type="agency", name="Интерфакс", handle_or_domain="interfax.ru")
    mash = Source(type="tg", name="Mash", handle_or_domain="@breakingmash")
    session.add_all([tass, ifx, mash])
    await session.flush()
    seed = await _doc(
        session, "https://t.me/breakingmash/1", 1.0, 30, mash, "Горит склад площадью 2000 кв. м"
    )
    await _doc(session, "https://tass.ru/1", 0.95, 0, tass, "В Подольске склад площадью 2000 кв. м")
    await _doc(
        session,
        "https://interfax.ru/1",
        0.94,
        5,
        ifx,
        "Сообщают: площадь пожара 3000 квадратных метров",
    )

    check = await run_check(session, f"doc:{seed.id}", FakeEmbedder(), llm=client(fake_model))
    analysis = json.loads(check.summary)["analysis"]

    [claim] = analysis["claims"]
    stances = {e["source"]: e["stance"] for e in claim["evidence"]}
    assert stances == {"ТАСС": "supports", "Интерфакс": "contradicts"}
    assert claim["independent_groups"] == 2
    assert analysis["index"] is not None and check.confirmation_index == analysis["index"]
    assert analysis["low_confidence"] == 0  # второй контур подтвердил противоречие
    assert await session.scalar(select(Claim.text)) == claim["text"]
    assert len((await session.scalars(select(ClaimEvidence))).all()) == 2
    stages = (
        await session.scalars(select(LlmCall.stage).where(LlmCall.check_id == check.id))
    ).all()
    assert "verify_disputed" in stages


async def test_disputed_contradiction_becomes_low_confidence(session: AsyncSession) -> None:
    ifx = Source(type="agency", name="Интерфакс", handle_or_domain="interfax.ru")
    session.add(ifx)
    await session.flush()
    seed = await _doc(session, "https://a.ru/1", 1.0, 30, None, "Горит склад площадью 2000 кв. м")
    await _doc(
        session,
        "https://interfax.ru/1",
        0.94,
        5,
        ifx,
        "Сообщают: площадь пожара 3000 квадратных метров",
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if json.loads(request.content)["model"] == "deepseek-v4-pro":
            return answer({"results": [{"claim": 1, "stance": "unrelated"}]})
        return fake_model(request)

    check = await run_check(session, f"doc:{seed.id}", FakeEmbedder(), llm=client(handler))
    analysis = json.loads(check.summary)["analysis"]

    [evidence] = analysis["claims"][0]["evidence"]
    assert evidence["low_confidence"] and evidence["stance"] == "contradicts"
    assert analysis["low_confidence"] == 1
    assert analysis["claims"][0]["independent_groups"] == 0  # в индекс не пошло


async def test_check_survives_unavailable_llm(session: AsyncSession) -> None:
    seed = await _doc(session, "https://a.ru/1", 1.0, text="Горит склад")

    llm = client(lambda request: httpx.Response(500))
    check = await run_check(session, f"doc:{seed.id}", FakeEmbedder(), llm=llm)
    report = json.loads(check.summary)

    assert check.status == "done"
    assert "error" in report["analysis"] and report["publications"] == 1


async def test_old_news_before_full_collection_is_archive_gap(session: AsyncSession) -> None:
    sources = [Source(type="media", name=f"s{i}", handle_or_domain=f"s{i}.ru") for i in range(4)]
    session.add_all(sources)
    await session.flush()
    seed = await _doc(session, "https://s0.ru/old", 1.0, -60 * 24, sources[0], "Горит склад")
    for s in sources[1:]:  # остальные источники пишут только сегодня
        await _doc(session, f"https://{s.handle_or_domain}/new", 0.0, 0, s, "Другое")

    def handler(request: httpx.Request) -> httpx.Response:
        if "<claims>" in json.loads(request.content)["messages"][1]["content"]:
            return answer({"results": [{"claim": 1, "stance": "unrelated"}]})
        return fake_model(request)

    check = await run_check(session, f"doc:{seed.id}", FakeEmbedder(), llm=client(handler))
    analysis = json.loads(check.summary)["analysis"]

    assert analysis["status"] == "archive_gap" and analysis["index"] is None
    assert "0 из 4 источников" in analysis["status_text"]  # сбор начат после новости


async def test_second_contour_compatible_verdict_replaces_contradiction(
    session: AsyncSession,
) -> None:
    ifx = Source(type="agency", name="Интерфакс", handle_or_domain="interfax.ru")
    session.add(ifx)
    await session.flush()
    seed = await _doc(session, "https://a.ru/1", 1.0, 30, None, "Горит склад площадью 2000 кв. м")
    await _doc(
        session,
        "https://interfax.ru/1",
        0.94,
        5,
        ifx,
        "Сообщают: площадь пожара 3000 квадратных метров",
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if json.loads(request.content)["model"] == "deepseek-v4-pro":
            # Вторая модель сама говорит «оба могут быть верны» — противоречием это не считается.
            return answer(
                {
                    "results": [
                        {
                            "claim": 1,
                            "both_can_be_true": True,
                            "stance": "contradicts",
                            "quote": "площадь пожара 3000 квадратных метров",
                        }
                    ]
                }
            )
        return fake_model(request)

    check = await run_check(session, f"doc:{seed.id}", FakeEmbedder(), llm=client(handler))
    analysis = json.loads(check.summary)["analysis"]

    [evidence] = analysis["claims"][0]["evidence"]
    assert evidence["stance"] == "refines" and not evidence["low_confidence"]
    assert analysis["low_confidence"] == 0
    assert analysis["claims"][0]["independent_groups"] == 1


def test_deepseek_cost_counts_cache_and_offpeak() -> None:
    from datetime import UTC, datetime

    from app.llm.config import get_llm_config

    config = get_llm_config()
    peak = datetime(2026, 10, 14, 7, tzinfo=UTC)  # среда, 07:00 UTC — пик
    night = datetime(2026, 10, 14, 20, tzinfo=UTC)
    weekend = datetime(2026, 10, 17, 7, tzinfo=UTC)  # суббота — весь день вне пика
    full = config.cost_usd("deepseek-flash", 1_000_000, 0, at=peak)
    assert full == 0.30
    assert config.cost_usd("deepseek-flash", 1_000_000, 0, at=night) == 0.15
    assert config.cost_usd("deepseek-flash", 1_000_000, 0, at=weekend) == 0.15
    # 900 тыс. из миллиона — из кэша: платим за 100 тыс. полных и 900 тыс. кэшированных
    cached = config.cost_usd("deepseek-flash", 1_000_000, 0, cached_in=900_000, at=peak)
    assert abs(cached - (0.03 + 0.9 * 0.006)) < 1e-9


async def test_dated_page_older_than_archive_says_so(session: AsyncSession) -> None:
    from datetime import date

    sources = [Source(type="media", name=f"o{i}", handle_or_domain=f"o{i}.ru") for i in range(3)]
    session.add_all(sources)
    await session.flush()
    seed = await _doc(session, "https://www.llnl.gov/article/1", 1.0, text="Fusion ignition")
    seed.published_at, seed.stated_date = None, date(2023, 3, 23)  # только дата на странице
    for s in sources:
        await _doc(session, f"https://{s.handle_or_domain}/new", 0.0, 0, s, "Другое")

    def handler(request: httpx.Request) -> httpx.Response:
        if "<claims>" in json.loads(request.content)["messages"][1]["content"]:
            return answer({"results": [{"claim": 1, "stance": "unrelated"}]})
        return fake_model(request)

    check = await run_check(session, f"doc:{seed.id}", FakeEmbedder(), llm=client(handler))
    analysis = json.loads(check.summary)["analysis"]

    assert analysis["status"] == "before_archive" and analysis["index"] is None
    assert "от 23.03.2023 старше архива" in analysis["status_text"]


async def test_event_reported_but_claim_absent(session: AsyncSession) -> None:
    from datetime import UTC, datetime, timedelta

    sources = [Source(type="media", name=f"fr{i}", handle_or_domain=f"fr{i}.com") for i in range(2)]
    session.add_all(sources)
    await session.flush()
    seed = await _doc(
        session, "https://t.me/bankrollo/1", 1.0, text="Громят всё, кроме полок со свининой"
    )
    seed.published_at = datetime.now(UTC)  # пост свежий, а история — двухдневная
    for i, s in enumerate(sources):
        d = await _doc(
            session, f"https://{s.handle_or_domain}/riots", 0.95, 0, s, "Погромы во Франции"
        )
        d.published_at = datetime.now(UTC) - timedelta(hours=48 - i)

    def handler(request: httpx.Request) -> httpx.Response:
        if "<claims>" in json.loads(request.content)["messages"][1]["content"]:
            return answer({"results": [{"claim": 1, "stance": "unrelated"}]})
        return fake_model(request)

    check = await run_check(session, f"doc:{seed.id}", FakeEmbedder(), llm=client(handler))
    analysis = json.loads(check.summary)["analysis"]
    assert analysis["status"] == "claim_absent" and analysis["index"] is None
    assert "о событии пишут, но этого утверждения в публикациях нет" in analysis["status_text"]
