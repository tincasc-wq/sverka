"""Сравнение языковых моделей на сверке утверждений: кто выдумывает цитаты, кто расходится.

Берём сохранённые проверки (утверждения + публикации, с которыми их сверял DeepSeek) и заново
сверяем те же пары каждой моделью из списка. Без записи в базу: в конце — откат.

Что считаем по каждой модели:
- выдуманные цитаты — модель привела «дословную» цитату, которой нет в тексте публикации
  (код ищет её по тексту; такие выводы конвейер отбрасывает) — прямая мера галлюцинаций;
- согласие с сохранённым выводом DeepSeek (supports / contradicts / …) и с «свой источник /
  пересказ»; DeepSeek в списке тоже есть — его согласие с самим собой и есть «шумовой пол»;
- сбои: не ответила, ответила не JSON-ом; время и токены (цена — если задана в llm.yaml).

    python -m app.quality.models yandex:yandexgpt/latest gigachat:GigaChat-2-Max \\
        [--checks 30] [--pace 18] [--json]

--pace — пауза между запросами к модели, с (кроме DeepSeek): у бесплатных шлюзов лимит частоты.

Модель задаётся как провайдер:модель из config/llm.yaml (deepseek, qwen, yandex, gigachat).
DeepSeek (deepseek:deepseek-flash, без рассуждений) добавляется сам, если его нет в списке.
"""

import asyncio
import json
import sys
import time
from collections import Counter
from dataclasses import asdict, dataclass, field

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import SessionLocal
from app.llm import LlmClient, LlmUnavailable
from app.llm.config import Route, get_llm_config
from app.models import Check, Claim, ClaimEvidence, Document
from app.pipeline.llm_steps import verify_document

BASELINE = "deepseek:deepseek-flash"
DECISIVE = ("supports", "contradicts", "corrects", "refines")


@dataclass
class ModelStats:
    model: str
    documents: int = 0  # сверено публикаций (вызовов модели)
    failed: int = 0  # модель не ответила или ответила не JSON-ом
    answers: int = 0  # выводов по утверждениям
    invented_quotes: int = 0  # цитаты, которых нет в тексте
    agree_stance: int = 0
    compared_stance: int = 0
    agree_sourcing: int = 0
    compared_sourcing: int = 0
    tokens_in: int = 0
    tokens_out: int = 0
    cost_usd: float = 0.0
    seconds: float = 0.0
    stances: Counter = field(default_factory=Counter)
    disagreements: list[str] = field(default_factory=list)

    def summary(self) -> dict:
        def share(a: int, b: int) -> float | None:
            return round(100 * a / b, 1) if b else None

        return {
            "модель": self.model,
            "публикаций": self.documents,
            "сбоев": self.failed,
            "выводов": self.answers,
            "выдуманных цитат, %": share(self.invented_quotes, self.answers + self.invented_quotes),
            "согласие с DeepSeek по выводу, %": share(self.agree_stance, self.compared_stance),
            "согласие по источнику, %": share(self.agree_sourcing, self.compared_sourcing),
            "секунд на публикацию": round(self.seconds / self.documents, 1)
            if self.documents
            else None,
            "токенов на публикацию": round((self.tokens_in + self.tokens_out) / self.documents)
            if self.documents
            else None,
            "цена, USD": round(self.cost_usd, 4),
            "выводы": dict(self.stances),
        }


def parse_model(spec: str) -> Route:
    provider, _, model = spec.partition(":")
    if not model:
        raise SystemExit(f"модель задаётся как провайдер:модель — «{spec}»")
    thinking = "disabled" if provider == "deepseek" else None
    return Route(provider=provider, model=model, thinking=thinking)


def client_for(route: Route) -> LlmClient:
    """Клиент, у которого шаг verify идёт только в эту модель — без резервных."""
    config = get_llm_config().model_copy(deep=True)
    config.routes = {"verify": [route]}
    config.daily_budget_usd = {}
    return LlmClient(config=config)


@dataclass
class Pair:
    check_id: int
    document: Document
    claims: list[str]
    baseline: dict[int, tuple[str, str | None]]  # номер утверждения → (вывод, источник)


async def load_pairs(session: AsyncSession, checks: int) -> list[Pair]:
    """Публикации из последних проверок, которые DeepSeek сверял с утверждениями новости."""
    check_ids = list(
        await session.scalars(
            select(Check.id)
            .join(Claim, Claim.check_id == Check.id)
            .where(Check.status == "done")
            .group_by(Check.id)
            .having(func.count(Claim.id) > 0)
            .order_by(Check.id.desc())
            .limit(checks)
        )
    )
    pairs: list[Pair] = []
    for check_id in check_ids:
        claims = list(
            await session.scalars(
                select(Claim).where(Claim.check_id == check_id).order_by(Claim.id)
            )
        )
        number = {c.id: i for i, c in enumerate(claims, 1)}
        rows = await session.execute(
            select(ClaimEvidence).where(ClaimEvidence.claim_id.in_(number))
        )
        by_doc: dict[int, dict[int, tuple[str, str | None]]] = {}
        for (e,) in rows:
            by_doc.setdefault(e.document_id, {})[number[e.claim_id]] = (e.stance, e.sourcing)
        for doc_id, baseline in by_doc.items():
            doc = await session.get(Document, doc_id)
            if doc is not None:
                pairs.append(Pair(check_id, doc, [c.text for c in claims], baseline))
    return pairs


def score(stats: ModelStats, pair: Pair, results, rejected: int) -> None:
    stats.invented_quotes += rejected
    for r in results:
        stats.answers += 1
        stats.stances[r.stance] += 1
        base = pair.baseline.get(r.claim)
        if base is None:
            continue
        stats.compared_stance += 1
        if r.stance == base[0]:
            stats.agree_stance += 1
        elif len(stats.disagreements) < 15:
            stats.disagreements.append(
                f"проверка {pair.check_id}, док {pair.document.id}, утв. {r.claim}: "
                f"DeepSeek {base[0]} → {r.stance}"
            )
        if r.stance in DECISIVE and base[0] in DECISIVE and base[1] and r.sourcing:
            stats.compared_sourcing += 1
            stats.agree_sourcing += r.sourcing == base[1]


async def _verify_paced(client, session, pair: Pair, pace: float):
    """Бесплатные шлюзы ограничивают частоту (Kilo без ключа — 200 в час): пауза между
    запросами и повтор, если шлюз ответил «слишком много запросов»."""
    for attempt in range(4):
        try:
            return await verify_document(client, session, pair.claims, pair.document)
        except LlmUnavailable as exc:
            if "429" not in str(exc) or attempt == 3:
                raise
            await asyncio.sleep(max(pace, 30) * (attempt + 2))
        finally:
            if pace:
                await asyncio.sleep(pace)


async def run_model(
    session: AsyncSession, spec: str, pairs: list[Pair], pace: float = 0
) -> ModelStats:
    route = parse_model(spec)
    client = client_for(route)
    stats = ModelStats(spec)
    for pair in pairs:
        started = time.monotonic()
        try:
            results, rejected = await _verify_paced(
                client, session, pair, pace if route.provider != "deepseek" else 0
            )
        except LlmUnavailable as exc:
            stats.failed += 1
            if stats.failed <= 3:
                print(f"  {spec}: сбой — {exc}"[:300], file=sys.stderr)
            continue
        finally:
            stats.seconds += time.monotonic() - started
        stats.documents += 1
        score(stats, pair, results, rejected)
    from app.models import LlmCall

    usage = (
        await session.execute(
            select(
                func.coalesce(func.sum(LlmCall.tokens_in), 0),
                func.coalesce(func.sum(LlmCall.tokens_out), 0),
                func.coalesce(func.sum(LlmCall.cost_usd), 0),
            ).where(
                LlmCall.provider == route.provider,
                LlmCall.model == route.model,
                LlmCall.check_id.is_(None),
                LlmCall.stage == "verify",
            )
        )
    ).one()
    stats.tokens_in, stats.tokens_out, stats.cost_usd = (
        int(usage[0]),
        int(usage[1]),
        float(usage[2]),
    )
    return stats


async def main(argv: list[str]) -> None:
    as_json = "--json" in argv
    checks = 30
    pace = 0.0
    specs: list[str] = []
    args = iter(a for a in argv if a != "--json")
    for a in args:
        if a == "--checks":
            checks = int(next(args))
        elif a == "--pace":  # секунд между запросами: у бесплатных шлюзов лимит частоты
            pace = float(next(args))
        else:
            specs.append(a)
    if BASELINE not in specs:
        specs.insert(0, BASELINE)
    async with SessionLocal() as session:
        pairs = await load_pairs(session, checks)
        print(f"Проверок: {checks}, публикаций для сверки: {len(pairs)}", file=sys.stderr)
        results = []
        for spec in specs:
            print(f"→ {spec}", file=sys.stderr)
            results.append(await run_model(session, spec, pairs, pace))
        await session.rollback()  # учёт вызовов не сохраняем: это замер, не работа сервиса
    if as_json:
        print(
            json.dumps(
                [asdict(s) | {"summary": s.summary()} for s in results],
                ensure_ascii=False,
                indent=2,
                default=str,
            )
        )
        return
    for s in results:
        print()
        for k, v in s.summary().items():
            print(f"{k:34} {v}")
        for line in s.disagreements[:8]:
            print(f"   ≠ {line}")


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1:]))
