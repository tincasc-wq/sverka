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


async def test_twins_reuse_vector_and_fresh_go_first(session: AsyncSession) -> None:
    from datetime import UTC, datetime, timedelta

    now = datetime.now(UTC)
    vector = [0.5] * EMBEDDING_DIM
    session.add_all(
        [
            # Уже посчитанный текст городского портала и его двойник на другом портале.
            Document(
                url="https://74.test/1",
                title="Осадки на Покров",
                text="Приметы",
                simhash=42,
                embedding=vector,
                first_seen_at=now - timedelta(hours=3),
            ),
            Document(
                url="https://ngs.test/1",
                title="Осадки на Покров",
                text="Приметы",
                simhash=42,
                first_seen_at=now - timedelta(hours=2),
            ),
            # Тот же отпечаток, другой заголовок — не двойник, считаем честно.
            Document(
                url="https://e1.test/1",
                title="Другое",
                text="Приметы",
                simhash=42,
                first_seen_at=now - timedelta(hours=1),
            ),
            Document(
                url="https://old.test/1",
                title="Старое",
                text="Вчера",
                first_seen_at=now - timedelta(days=1),
            ),
        ]
    )
    await session.flush()
    embedder = FakeEmbedder()

    assert await embed_pending(session, embedder, limit=2) == 2

    # Сначала самые свежие; двойник получил готовый вектор без вызова модели.
    assert embedder.calls == [["Другое\nПриметы"]]
    twin = await session.scalar(select(Document).where(Document.url == "https://ngs.test/1"))
    assert list(twin.embedding) == vector
    old = await session.scalar(select(Document).where(Document.url == "https://old.test/1"))
    assert old.embedding is None


async def test_budget_stops_between_batches(session: AsyncSession, monkeypatch) -> None:
    from app.core.config import get_settings

    monkeypatch.setattr(get_settings(), "embedding_batch_size", 1)
    session.add_all([Document(url=f"https://b.test/{i}", text=f"Текст {i}") for i in range(3)])
    await session.flush()

    # Бюджет 0: первая пачка идёт (проверка — до неё), дальше — стоп, без ожидания таймаута.
    assert await embed_pending(session, FakeEmbedder(), budget_seconds=-1) == 0
    assert await embed_pending(session, FakeEmbedder(), budget_seconds=60) == 3
