"""Эмбеддинги публикаций для смыслового поиска (bge-m3, 1024 измерения, локально)."""

import asyncio
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

            # Оставляем ядра сборщикам и БД; длиннее 512 токенов для поиска похожих не нужно.
            torch.set_num_threads(get_settings().embedding_threads)
            self._model = SentenceTransformer(self.model_name, device="cpu")
            self._model.max_seq_length = 512
        vectors = self._model.encode(texts, normalize_embeddings=True, batch_size=len(texts))
        return [v.tolist() for v in vectors]


def document_text(doc: Document, max_chars: int) -> str:
    return "\n".join(p for p in (doc.title, doc.text) if p)[:max_chars]


async def embed_pending(session: AsyncSession, embedder: Embedder, limit: int = 256) -> int:
    """Считает эмбеддинги для документов без них. Возвращает число обработанных."""
    settings = get_settings()
    docs = list(
        await session.scalars(
            select(Document)
            .where(Document.embedding.is_(None), Document.text.is_not(None))
            .order_by(Document.first_seen_at)
            .limit(limit)
        )
    )
    batch = settings.embedding_batch_size
    for i in range(0, len(docs), batch):
        chunk = docs[i : i + batch]
        texts = [document_text(d, settings.embedding_max_chars) for d in chunk]
        vectors = await asyncio.to_thread(embedder.embed, texts)
        for doc, vector in zip(chunk, vectors, strict=True):
            doc.embedding = vector
        await session.commit()
    return len(docs)
