from datetime import datetime

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import EMBEDDING_DIM, Base, CreatedAtMixin


class Topic(CreatedAtMixin, Base):
    """Тема мониторинга организации: бренд, персона, ведомство, сюжет (app/topics.py).

    Публикация попадает в тему, если в ней есть одно из ключевых слов (с учётом окончаний)
    или если она близка по смыслу к описанию темы; слова-исключения отбрасывают находку.
    """

    __tablename__ = "topics"
    __table_args__ = (CheckConstraint("mode IN ('instant', 'digest', 'spikes')", name="mode"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    org_id: Mapped[int] = mapped_column(ForeignKey("organizations.id"), index=True)
    created_by: Mapped[int] = mapped_column(ForeignKey("users.id"))
    name: Mapped[str] = mapped_column(String(255))
    keywords: Mapped[list[str]] = mapped_column(JSONB, server_default="[]", default=list)
    exclude: Mapped[list[str]] = mapped_column(JSONB, server_default="[]", default=list)
    description: Mapped[str | None] = mapped_column(Text)
    description_vec: Mapped[list[float] | None] = mapped_column(Vector(EMBEDDING_DIM))
    # Сколько похожести на описание достаточно (косинус bge-m3); 0.6 — без заметного шума.
    similarity: Mapped[float] = mapped_column(Float, server_default="0.6", default=0.6)
    # instant — сразу (не чаще раза в 30 минут), digest — сводка раз в день, spikes — только
    # всплески. О всплеске пишем в любом режиме.
    mode: Mapped[str] = mapped_column(String(16), server_default="instant", default="instant")
    digest_hour: Mapped[int] = mapped_column(Integer, server_default="9", default=9)  # МСК
    active: Mapped[bool] = mapped_column(Boolean, server_default="true", default=True)
    scanned_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_spike_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    note: Mapped[str | None] = mapped_column(Text)  # предупреждение: «слишком широкая тема»


class TopicHit(Base):
    """Публикация, попавшая в тему."""

    __tablename__ = "topic_hits"
    __table_args__ = (
        UniqueConstraint("topic_id", "document_id"),
        Index("ix_topic_hits_topic_found", "topic_id", "found_at"),
        CheckConstraint("matched IN ('keyword', 'meaning')", name="matched"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    topic_id: Mapped[int] = mapped_column(ForeignKey("topics.id", ondelete="CASCADE"))
    document_id: Mapped[int] = mapped_column(ForeignKey("documents.id"))
    matched: Mapped[str] = mapped_column(String(16))
    keyword: Mapped[str | None] = mapped_column(Text)  # какое слово нашлось
    score: Mapped[float | None] = mapped_column(Float)  # похожесть на описание
    found_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    notified: Mapped[bool] = mapped_column(Boolean, server_default="false", default=False)
    # Главная находка (тема в заголовке, в начале или упомянута трижды) или упоминание вскользь.
    prominent: Mapped[bool] = mapped_column(Boolean, server_default="true", default=True)


class TopicSubscriber(Base):
    """Кто из команды получает оповещения по теме (автор подписан сразу)."""

    __tablename__ = "topic_subscribers"
    __table_args__ = (UniqueConstraint("topic_id", "user_id"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    topic_id: Mapped[int] = mapped_column(ForeignKey("topics.id", ondelete="CASCADE"), index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
