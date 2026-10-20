"""Поиск по каждому утверждению и честный итог для текста (случай с амишами, 20.10.2026).

Длинный текст о нескольких событиях (школы амишей, суды, корь в Нью-Йорке) целиком не похож
ни на одну публикацию — сюжет оставался пустым, хотя о чрезвычайном положении из-за кори
писала The Hindu. Теперь для каждого утверждения ищем в архиве ближайшие публикации.
"""

import json
from datetime import UTC, datetime

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from app.check import run_check
from app.models import Document, Source
from app.models.base import EMBEDDING_DIM
from tests.test_llm import answer, client
from tests.test_story import vec

CLAIM = "Губернатор Нью-Йорка объявила чрезвычайное положение из-за вспышки кори"
HINDU = "New York declares disaster emergency over measles outbreak, Governor Hochul said."


class ClaimEmbedder:
    """Утверждение о кори смотрит туда же, куда заметка The Hindu (ось 7)."""

    def embed(self, texts: list[str]) -> list[list[float]]:
        out = []
        for t in texts:
            v = [0.0] * EMBEDDING_DIM
            v[7 if "кори" in t else 9] = 1.0
            out.append(v)
        return out


def model(request: httpx.Request) -> httpx.Response:
    user = json.loads(request.content)["messages"][1]["content"]
    if "Кандидаты:" in user:
        return answer({"decisions": []})
    if "<claims>" not in user:
        return answer({"claims": [{"text": CLAIM}]})
    doc = user.split("<document>")[1]
    if "measles" in doc:
        quote = "New York declares disaster emergency over measles outbreak"
        return answer(
            {"results": [{"claim": 1, "stance": "supports", "sourcing": "own", "quote": quote}]}
        )
    return answer({"results": [{"claim": 1, "stance": "unrelated"}]})


async def _text_seed(session: AsyncSession, text: str) -> Document:
    seed = Document(url="user-text://amish", text=text, embedding=vec(1.0, axis=3))
    session.add(seed)
    await session.flush()
    return seed


async def test_claim_finds_evidence_outside_story(session: AsyncSession) -> None:
    hindu = Source(type="media", name="The Hindu", handle_or_domain="thehindu.com", lang="en")
    session.add(hindu)
    await session.flush()
    v = [0.0] * EMBEDDING_DIM
    v[7] = 1.0
    session.add(
        Document(
            url="https://thehindu.com/measles",
            source_id=hindu.id,
            title="New York declares disaster emergency over measles outbreak",
            text=HINDU,
            embedding=v,
            published_at=datetime.now(UTC),
        )
    )
    seed = await _text_seed(session, "Амиши против прививок. " + CLAIM + ". Школы закрыты.")
    check = await run_check(session, f"doc:{seed.id}", ClaimEmbedder(), llm=client(model))
    analysis = json.loads(check.summary)["analysis"]
    [claim] = analysis["claims"]
    assert [e["source"] for e in claim["evidence"]] == ["The Hindu"]
    assert claim["evidence"][0]["stance"] == "supports"


async def test_text_without_matches_is_not_too_early(session: AsyncSession) -> None:
    seed = await _text_seed(session, "Амиши против прививок. " + CLAIM + ".")
    check = await run_check(session, f"doc:{seed.id}", ClaimEmbedder(), llm=client(model))
    analysis = json.loads(check.summary)["analysis"]
    assert analysis["status"] == "not_found_text"
    assert analysis["status_text"].startswith("в архиве не найдено")


async def test_short_caption_over_image_gets_hint(session: AsyncSession, monkeypatch) -> None:
    # Шаранизм, 20.10.2026: «Кляти москали!» над скриншотом статьи — вся новость на картинке.
    from app import post_images

    async def yes(url: str) -> bool:
        return True

    monkeypatch.setattr(post_images, "post_has_images", yes)
    assert post_images.short_caption("Кляти москали!")
    assert not post_images.short_caption("Слово " * 20)
    post = Document(url="https://t.me/sharanism/62457", text="Кляти москали!", embedding=vec(1.0))
    session.add(post)
    await session.flush()
    check = await run_check(session, f"doc:{post.id}", ClaimEmbedder(), llm=client(model))
    assert "на картинке" in json.loads(check.summary)["image_hint"]
