from datetime import date, datetime

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import EMBEDDING_DIM, Base, enum_check
from app.models.enums import ChangeType, DerivationKind, StoryStatus


class Document(Base):
    """Любая собранная публикация (статья, пост)."""

    __tablename__ = "documents"
    __table_args__ = (
        Index(
            "ix_documents_title_trgm",
            "title",
            postgresql_using="gin",
            postgresql_ops={"title": "gin_trgm_ops"},
        ),
        # ILIKE '%имя%' по тексту — поиск соседних эпизодов по редким именам.
        Index(
            "ix_documents_text_trgm",
            "text",
            postgresql_using="gin",
            postgresql_ops={"text": "gin_trgm_ops"},
        ),
        Index(
            "ix_documents_embedding_hnsw",
            "embedding",
            postgresql_using="hnsw",
            postgresql_ops={"embedding": "vector_cosine_ops"},
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    source_id: Mapped[int | None] = mapped_column(ForeignKey("sources.id"), index=True)
    url: Mapped[str] = mapped_column(Text, unique=True)
    title: Mapped[str | None] = mapped_column(Text)
    text: Mapped[str | None] = mapped_column(Text)
    lang: Mapped[str | None] = mapped_column(String(8))
    # Дата, которую заявляет сам источник.
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Дата без времени со страницы («March 23, 2023»): для первоисточника не годится
    # (полночь сделала бы статью ложно самой ранней), но показывает, что статья старая.
    stated_date: Mapped[date | None] = mapped_column(Date)
    # Наше доверенное время: когда сборщик впервые увидел публикацию. Не меняется (триггер).
    first_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )
    embedding: Mapped[list[float] | None] = mapped_column(Vector(EMBEDDING_DIM))
    # Исходящие ссылки на другие сайты и каналы (нормализованные) — рёбра explicit_link (6.3).
    links: Mapped[list[str]] = mapped_column(JSONB, server_default="[]", default=list)
    content_hash: Mapped[str | None] = mapped_column(String(64), index=True)
    simhash: Mapped[int | None] = mapped_column(BigInteger, index=True)


class DocumentVersion(Base):
    """Правки и удаления публикаций (ФТ-3.3)."""

    __tablename__ = "document_versions"
    __table_args__ = (enum_check("change_type", ChangeType),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    document_id: Mapped[int] = mapped_column(ForeignKey("documents.id"), index=True)
    text: Mapped[str | None] = mapped_column(Text)
    seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    change_type: Mapped[str] = mapped_column(String(16))


class Story(Base):
    """Кластер публикаций об одном событии."""

    __tablename__ = "stories"
    __table_args__ = (enum_check("status", StoryStatus),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    title: Mapped[str | None] = mapped_column(Text)
    centroid: Mapped[list[float] | None] = mapped_column(Vector(EMBEDDING_DIM))
    first_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    status: Mapped[str] = mapped_column(String(16), server_default=StoryStatus.OPEN.value)


class StoryDocument(Base):
    __tablename__ = "story_documents"
    __table_args__ = (
        CheckConstraint("membership IN ('core', 'probable')", name="membership"),
        CheckConstraint(
            "analyst_stance IS NULL OR analyst_stance IN "
            "('supports', 'contradicts', 'refines', 'unrelated')",
            name="analyst_stance",
        ),
    )

    story_id: Mapped[int] = mapped_column(ForeignKey("stories.id"), primary_key=True)
    document_id: Mapped[int] = mapped_column(ForeignKey("documents.id"), primary_key=True)
    similarity: Mapped[float | None] = mapped_column(Float)
    # core — точно то же событие; probable — средняя близость, нужна проверка (6.2).
    membership: Mapped[str] = mapped_column(String(16), server_default="core")
    # Решение аналитика: публикация распространяла утверждение, опровергала его или не по теме.
    # Пусто — «распространяла» (так рейтинг считает по умолчанию).
    analyst_stance: Mapped[str | None] = mapped_column(String(16))


class Derivation(Base):
    """Ребро графа заимствований: to_doc взял у from_doc."""

    __tablename__ = "derivations"
    __table_args__ = (
        enum_check("kind", DerivationKind),
        UniqueConstraint("from_doc_id", "to_doc_id", "kind", name="uq_derivations_edge"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    from_doc_id: Mapped[int] = mapped_column(ForeignKey("documents.id"), index=True)
    to_doc_id: Mapped[int] = mapped_column(ForeignKey("documents.id"), index=True)
    kind: Mapped[str] = mapped_column(String(16))
    confidence: Mapped[float] = mapped_column(Float)
