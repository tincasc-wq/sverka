"""LLM-шаги конвейера (ТЗ 6.1, 6.2 п.3, 6.4): одно ли событие, утверждения, сверка.

LLM делает только лингвистическую работу; итоговые цифры считает код (app/pipeline/index.py).
Любой вывод сверки опирается на дословную цитату, которую код ищет в тексте публикации:
нет цитаты в тексте — вывод отбрасывается (защита от галлюцинаций, ФТ-7.3).
"""

import asyncio
import re
from typing import Literal
from zoneinfo import ZoneInfo

from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.llm import LlmClient
from app.llm.prompts import load_prompt
from app.models import Document

SAME_EVENT_BATCH = 12
SNIPPET_CHARS = 700
VERIFY_DOC_CHARS = 4000
MAX_CLAIMS = 7
VERIFY_CONCURRENCY = 6
MSK = ZoneInfo("Europe/Moscow")


# ---- схемы ответов ---------------------------------------------------------------------


class Decision(BaseModel):
    id: int
    same_event: bool
    reason: str = ""


class SameEventAnswer(BaseModel):
    seed_event: str = ""  # модель сначала называет главный факт — так она реже путает фон
    decisions: list[Decision]


class ClaimOut(BaseModel):
    text: str = Field(min_length=5)
    subject: str | None = None
    date: str | None = None
    place: str | None = None
    numbers: list[str] = []
    attributed_to: str | None = None
    checkable: bool = True


class ClaimsAnswer(BaseModel):
    claims: list[ClaimOut]


class VerifyResult(BaseModel):
    claim: int
    stance: Literal["supports", "contradicts", "corrects", "refines", "unrelated"]
    # own — свой источник; retelling — пересказ. Пересказ подтверждением не считается.
    sourcing: Literal["own", "retelling"] | None = None
    quote: str | None = None
    discrepancy: str | None = None
    # Второй контур явно отвечает «могут ли оба быть верны одновременно»; да — не противоречие.
    both_can_be_true: bool | None = None


class VerifyAnswer(BaseModel):
    results: list[VerifyResult]


# ---- вспомогательное -------------------------------------------------------------------


def _published(doc: Document) -> str:
    moment = doc.published_at or doc.first_seen_at
    return moment.astimezone(MSK).strftime("%Y-%m-%d %H:%M МСК") if moment else "неизвестно"


def snippet(doc: Document, limit: int) -> str:
    text = "\n".join(p for p in (doc.title, doc.text) if p)
    return " ".join(text.split())[:limit]


_QUOTES = str.maketrans({"«": '"', "»": '"', "“": '"', "”": '"', "„": '"', "ё": "е", "Ё": "Е"})
_SPACE = re.compile(r"\s+")


def _norm(text: str) -> str:
    return _SPACE.sub(" ", text.translate(_QUOTES).lower()).strip(" .,;:")


def quote_found(quote: str | None, text: str | None) -> bool:
    """Цитата дословно есть в тексте — с точностью до регистра, кавычек, ё и пробелов."""
    if not quote or not text:
        return False
    q = _norm(quote)
    return len(q.split()) >= 3 and q in _norm(text)


# ---- шаги ------------------------------------------------------------------------------


async def judge_same_event(
    client: LlmClient,
    session: AsyncSession,
    seed: Document,
    candidates: list[Document],
    *,
    check_id: int | None = None,
) -> dict[int, Decision]:
    """Решение «то же событие?» по каждому кандидату (6.2 п.3). Ключ — id документа.

    Батчи независимы и идут параллельно (замер 12.11.2026: большие истории ждали
    4 последовательных батча по ~3 с); решения склеиваются по id документа.
    """
    prompt = load_prompt("same_event")
    semaphore = asyncio.Semaphore(VERIFY_CONCURRENCY)

    async def one(batch: list[Document]) -> dict[int, Decision]:
        listing = "\n".join(
            f'<document id="{i}" published="{_published(doc)}">'
            f"{snippet(doc, SNIPPET_CHARS)}</document>"
            for i, doc in enumerate(batch, 1)
        )
        user = (
            f'Исходная публикация:\n<document published="{_published(seed)}">'
            f"{snippet(seed, SNIPPET_CHARS * 2)}</document>\n\nКандидаты:\n{listing}"
        )
        async with semaphore:
            answer = await client.complete_json(
                session,
                "same_event",
                prompt.text,
                user,
                SameEventAnswer,
                check_id=check_id,
                prompt_version=prompt.version,
            )
        return {batch[d.id - 1].id: d for d in answer.decisions if 1 <= d.id <= len(batch)}

    batches = [
        candidates[s : s + SAME_EVENT_BATCH] for s in range(0, len(candidates), SAME_EVENT_BATCH)
    ]
    decisions: dict[int, Decision] = {}
    for part in await asyncio.gather(*(one(b) for b in batches)):
        decisions.update(part)
    return decisions


async def extract_claims(
    client: LlmClient, session: AsyncSession, doc: Document, *, check_id: int | None = None
) -> list[ClaimOut]:
    prompt = load_prompt("extract_claims")
    answer = await client.complete_json(
        session,
        "extract_claims",
        prompt.text,
        f"<document>{snippet(doc, VERIFY_DOC_CHARS)}</document>",
        ClaimsAnswer,
        check_id=check_id,
        prompt_version=prompt.version,
    )
    return answer.claims[:MAX_CLAIMS]


async def verify_document(
    client: LlmClient,
    session: AsyncSession,
    claims: list[str],
    doc: Document,
    *,
    step: str = "verify",
    check_id: int | None = None,
    claims_from: Document | None = None,
) -> tuple[list[VerifyResult], int]:
    """Сверка всех утверждений с одной публикацией. Возвращает результаты и число отброшенных."""
    # Второй контур проверяет только «могут ли оба быть верны одновременно» — свой промпт.
    prompt = load_prompt("dispute" if step == "verify_disputed" else "verify")
    numbered = "\n".join(f"{i}. {c}" for i, c in enumerate(claims, 1))
    body = snippet(doc, VERIFY_DOC_CHARS)
    # Время обеих публикаций: ситуация развивается (нарастающий счёт, этапы дела), и разные
    # цифры в разное время — не противоречие (прогон качества 13.10.2026).
    when = f" (опубликована {_published(claims_from)})" if claims_from is not None else ""
    user = (
        f"Утверждения из новости{when}:\n<claims>\n{numbered}\n</claims>\n\n"
        f"Публикация (опубликована {_published(doc)}):\n<document>{body}</document>"
    )
    answer = await client.complete_json(
        session,
        step,
        prompt.text,
        user,
        VerifyAnswer,
        check_id=check_id,
        prompt_version=prompt.version,
    )
    full_text = "\n".join(p for p in (doc.title, doc.text) if p)
    results, rejected = [], 0
    for r in answer.results:
        if not 1 <= r.claim <= len(claims):
            continue
        if r.stance != "unrelated" and not quote_found(r.quote, full_text):
            # Вывод без дословной цитаты из публикации не принимаем (ТЗ 6.4).
            rejected += 1
            r = VerifyResult(claim=r.claim, stance="unrelated")
        elif r.stance in ("contradicts", "corrects") and r.both_can_be_true:
            # Модель сама признала, что оба факта совместимы, — это уточнение, а не противоречие.
            r = VerifyResult(claim=r.claim, stance="refines", quote=r.quote, sourcing=r.sourcing)
        results.append(r)
    return results, rejected


async def verify_many(
    client: LlmClient,
    session: AsyncSession,
    claims: list[str],
    docs: list[Document],
    *,
    step: str = "verify",
    check_id: int | None = None,
    claims_from: Document | None = None,
) -> dict[int, tuple[list[VerifyResult], int]]:
    """Сверка с несколькими публикациями параллельно; упавшая публикация просто пропускается."""
    semaphore = asyncio.Semaphore(VERIFY_CONCURRENCY)

    async def one(doc: Document):
        async with semaphore:
            try:
                return doc.id, await verify_document(
                    client,
                    session,
                    claims,
                    doc,
                    step=step,
                    check_id=check_id,
                    claims_from=claims_from,
                )
            except Exception:  # noqa: BLE001 — одна публикация не должна ронять проверку
                return doc.id, None

    pairs = await asyncio.gather(*(one(d) for d in docs))
    return {doc_id: result for doc_id, result in pairs if result is not None}
