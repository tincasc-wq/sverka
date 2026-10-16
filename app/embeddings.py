"""Эмбеддинги публикаций для смыслового поиска (bge-m3, 1024 измерения, локально)."""

import asyncio
import time
from datetime import datetime
from typing import Protocol

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.models import Document


class Embedder(Protocol):
    def embed(self, texts: list[str]) -> list[list[float]]: ...


class SentenceTransformerEmbedder:
    """Модель грузится при первом вызове: ~2 ГБ, только в образе воркера (extra «ml»)."""

    def __init__(self, model_name: str | None = None) -> None:
        self.model_name = model_name or get_settings().embedding_model
        self._model = None

    def embed(self, texts: list[str]) -> list[list[float]]:
        if self._model is None:
            import torch
            from sentence_transformers import SentenceTransformer

            # Оставляем ядро сборщикам и БД; длина — embedding_max_tokens (см. настройки).
            torch.set_num_threads(get_settings().embedding_threads)
            self._model = SentenceTransformer(self.model_name, device="cpu")
            self._model.max_seq_length = get_settings().embedding_max_tokens
        vectors = self._model.encode(texts, normalize_embeddings=True, batch_size=len(texts))
        return [v.tolist() for v in vectors]


# Срочные сообщения агентств («молнии» ТАСС) приходят одним заголовком: это тоже новость,
# и без вектора она невидима для поиска похожих (АЭС на Луне, 15.10.2026: ТАСС, первым
# сообщивший новость, не вошёл в историю). Вектор считаем по заголовку или тексту.
HAS_CONTENT = Document.text.is_not(None) | Document.title.is_not(None)


def document_text(doc: Document, max_chars: int) -> str:
    return "\n".join(p for p in (doc.title, doc.text) if p)[:max_chars]


async def _copy_from_twins(session: AsyncSession, docs: list[Document]) -> list[Document]:
    """Одинаковые тексты (городские порталы одной сети публикуют одно и то же по 16 раз)
    получают уже посчитанный вектор двойника. Возвращает тех, кому считать всё же нужно."""
    rest = []
    for doc in docs:
        twin = None
        if doc.simhash is not None:
            twin = await session.scalar(
                select(Document.embedding)
                .where(
                    Document.simhash == doc.simhash,
                    Document.title.is_not_distinct_from(doc.title),
                    Document.embedding.is_not(None),
                    Document.id != doc.id,
                )
                .limit(1)
            )
        if twin is not None:
            doc.embedding = twin
        else:
            rest.append(doc)
    return rest


async def embed_pending(
    session: AsyncSession,
    embedder: Embedder,
    limit: int = 256,
    budget_seconds: float | None = None,
) -> int:
    """Считает эмбеддинги для документов без них. Возвращает число обработанных.

    Сначала свежие: проверяют обычно новости последних часов, а хвост подождёт.
    budget_seconds — остановиться между пачками, не дожидаясь таймаута задачи: отменённый
    по таймауту расчёт продолжал крутиться в потоке, следующие запуски множили такие потоки,
    и 15.10.2026 они задушили процессор — 9 тыс. публикаций за день остались без векторов.
    """
    settings = get_settings()
    docs = list(
        await session.scalars(
            select(Document)
            .where(Document.embedding.is_(None), HAS_CONTENT)
            .order_by(Document.first_seen_at.desc())
            .limit(limit)
        )
    )
    started = time.monotonic()
    done = 0
    batch = settings.embedding_batch_size
    for i in range(0, len(docs), batch):
        if budget_seconds is not None and time.monotonic() - started > budget_seconds:
            break
        chunk = docs[i : i + batch]
        todo = await _copy_from_twins(session, chunk)
        if todo:
            texts = [document_text(d, settings.embedding_max_chars) for d in todo]
            vectors = await asyncio.to_thread(embedder.embed, texts)
            for doc, vector in zip(todo, vectors, strict=True):
                doc.embedding = vector
        await session.commit()
        done += len(chunk)
    return done


# Очередь больше этого — расчёт не успевает за притоком: проверки начинают «не видеть»
# свежие публикации (15.10.2026 так пропала история про АЭС на Луне).
BACKLOG_ALERT = 1500


async def embedding_backlog(session: AsyncSession) -> tuple[int, datetime | None]:
    """(сколько публикаций ждут вектора, когда появилась самая старая из них)."""
    from sqlalchemy import func

    count, oldest = (
        await session.execute(
            select(func.count(), func.min(Document.first_seen_at)).where(
                Document.embedding.is_(None), HAS_CONTENT
            )
        )
    ).one()
    return int(count or 0), oldest
