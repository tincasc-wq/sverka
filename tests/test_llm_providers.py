"""GigaChat и Yandex в клиенте LLM; подсчёт метрик сравнения моделей."""

import json

import httpx
from pydantic import BaseModel

from app.llm.client import LlmClient, strip_fences
from app.llm.config import Route, get_llm_config
from app.models import Document
from app.pipeline.llm_steps import VerifyResult
from app.quality.models import ModelStats, Pair, parse_model, score


class Answer(BaseModel):
    ok: bool


def test_strip_fences() -> None:
    assert strip_fences('```json\n{"ok": true}\n```') == '{"ok": true}'
    assert strip_fences('{"ok": true}') == '{"ok": true}'


def _client(route: Route, handler) -> LlmClient:
    config = get_llm_config().model_copy(deep=True)
    config.routes = {"verify": [route]}
    config.daily_budget_usd = {}
    return LlmClient(
        config=config,
        transport=httpx.MockTransport(handler),
        api_keys={"gigachat": "QmFzaWM=", "yandex": "yandex-key"},
    )


async def test_gigachat_gets_token_and_skips_json_mode(session, monkeypatch) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path.endswith("/oauth"):
            return httpx.Response(200, json={"access_token": "tok-1", "expires_at": 9999999999999})
        body = json.loads(request.content)
        assert "response_format" not in body  # GigaChat его не понимает
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": '```json\n{"ok": true}\n```'}}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 2},
            },
        )

    client = _client(Route(provider="gigachat", model="GigaChat-2-Max"), handler)
    first = await client.complete_json(session, "verify", "sys", "user", Answer)
    await client.complete_json(session, "verify", "sys", "user", Answer)

    assert first.ok is True
    oauth = [r for r in seen if r.url.path.endswith("/oauth")]
    assert len(oauth) == 1  # токен переиспользуется, пока не истёк
    assert oauth[0].headers["authorization"] == "Basic QmFzaWM=" and "rquid" in oauth[0].headers
    assert seen[1].headers["authorization"] == "Bearer tok-1"


async def test_yandex_model_uri_and_folder(session, monkeypatch) -> None:
    from app.core.config import get_settings

    monkeypatch.setattr(get_settings(), "yandex_folder_id", "b1gfolder")
    seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append({"body": json.loads(request.content), "headers": dict(request.headers)})
        return httpx.Response(200, json={"choices": [{"message": {"content": '{"ok": true}'}}]})

    client = _client(Route(provider="yandex", model="yandexgpt/latest"), handler)
    await client.complete_json(session, "verify", "sys", "user", Answer)
    assert seen[0]["body"]["model"] == "gpt://b1gfolder/yandexgpt/latest"
    assert seen[0]["headers"]["x-folder-id"] == "b1gfolder"


def test_comparison_metrics() -> None:
    pair = Pair(
        1,
        Document(id=5, url="u"),
        ["a", "b", "c"],
        {1: ("supports", "own"), 2: ("unrelated", None), 3: ("supports", "retelling")},
    )
    stats = ModelStats("x:y")
    results = [
        VerifyResult(claim=1, stance="supports", quote="q", sourcing="own"),
        VerifyResult(claim=2, stance="contradicts", quote="q", sourcing="own"),
        VerifyResult(claim=3, stance="supports", quote="q", sourcing="own"),
    ]
    score(stats, pair, results, rejected=1)
    s = stats.summary()
    assert stats.agree_stance == 2 and stats.compared_stance == 3
    assert stats.agree_sourcing == 1 and stats.compared_sourcing == 2
    assert (
        s["выдуманных цитат, %"] == 25.0
        and "DeepSeek unrelated → contradicts" in stats.disagreements[0]
    )
    assert parse_model("deepseek:deepseek-flash").thinking == "disabled"
    assert parse_model("gigachat:GigaChat-2-Max").provider == "gigachat"


async def test_keyless_provider_sends_no_authorization(session) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"choices": [{"message": {"content": '{"ok": true}'}}]})

    config = get_llm_config().model_copy(deep=True)
    config.routes = {"verify": [Route(provider="kilo", model="nvidia/nemotron-3-super:free")]}
    config.daily_budget_usd = {}
    client = LlmClient(config=config, transport=httpx.MockTransport(handler), api_keys={})
    answer = await client.complete_json(session, "verify", "sys", "user", Answer)
    assert answer.ok and "authorization" not in seen[0].headers
    assert str(seen[0].url).startswith("https://ai-gateway.kilo.ai/api/v1/")


def test_kilo_is_not_in_production_routes() -> None:
    config = get_llm_config()
    for routes in (*config.routes.values(), *config.ru_only_routes.values()):
        assert all(r.provider not in ("kilo", "gigachat") for r in routes)
