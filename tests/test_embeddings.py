from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.embeddings import embed_pending
from app.models import Document
from app.models.base import EMBEDDING_DIM


class FakeEmbedder:
    """Детерминированный «эмбеддер»: настоящая модель весит ~2 ГБ и в тестах не нужна."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def embed(self, texts: list[str]) -> list[list[float]]:
        self.calls.append(texts)
        return [[float(len(t))] + [0.0] * (EMBEDDING_DIM - 1) for t in texts]


async def test_embed_pending_fills_only_missing_vectors(session: AsyncSession) -> None:
    session.add_all(
        [
            Document(url="https://e.ru/1", title="Заголовок", text="Текст"),
            Document(url="https://e.ru/2", text=None),  # без текста — пропускаем
            Document(url="https://e.ru/3", text="Уже есть", embedding=[1.0] * EMBEDDING_DIM),
        ]
    )
    await session.flush()
    embedder = FakeEmbedder()

    count = await embed_pending(session, embedder)

    assert count == 1
    assert embedder.calls == [["Заголовок\nТекст"]]
    doc = await session.scalar(select(Document).where(Document.url == "https://e.ru/1"))
    assert doc.embedding is not None
