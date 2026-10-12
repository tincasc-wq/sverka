from datetime import datetime

from sqlalchemy import BigInteger, Boolean, DateTime, Float, ForeignKey, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, CreatedAtMixin, enum_check
from app.models.enums import DecidedBy, SourceType, Verdict


class Source(CreatedAtMixin, Base):
    """Реестр источников (ФТ-6.1)."""

    __tablename__ = "sources"
    __table_args__ = (enum_check("type", SourceType),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    type: Mapped[str] = mapped_column(String(16))
    name: Mapped[str] = mapped_column(String(255))
    handle_or_domain: Mapped[str] = mapped_column(String(255), unique=True)
    owner: Mapped[str | None] = mapped_column(String(255))
    affiliation: Mapped[str | None] = mapped_column(String(255))
    # Иноагент, блокировка РКН и т.п. — только как факт, не влияет на рейтинг автоматически.
    legal_flags: Mapped[dict] = mapped_column(JSONB, server_default="{}", default=dict)
    is_tracked: Mapped[bool] = mapped_column(Boolean, server_default="true", default=True)
    # Как источник называют в чужих текстах: «по данным ТАСС», «сообщает „Коммерсантъ“» (6.3).
    aliases: Mapped[list[str]] = mapped_column(JSONB, server_default="[]", default=list)
    lang: Mapped[str] = mapped_column(String(8), server_default="ru", default="ru")
    # Опрашивать через шлюз (COLLECTOR_PROXY): сайт недоступен из РФ или не пускает
    # российские адреса. Сам факт блокировки — в legal_flags.
    via_gateway: Mapped[bool] = mapped_column(Boolean, server_default="false", default=False)

    # Состояние сборщика: RSS-лента (для tg/vk адрес берётся из handle_or_domain).
    feed_url: Mapped[str | None] = mapped_column(Text)
    last_polled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)
    consecutive_errors: Mapped[int] = mapped_column(Integer, server_default="0", default=0)


class SourceScore(Base):
    """История рейтинга источника. Только дополняется (триггер в миграции)."""

    __tablename__ = "source_scores"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    source_id: Mapped[int] = mapped_column(ForeignKey("sources.id"), index=True)
    score: Mapped[float] = mapped_column(Float)
    ci_low: Mapped[float] = mapped_column(Float)
    ci_high: Mapped[float] = mapped_column(Float)
    n_observations: Mapped[int] = mapped_column(Integer)
    reason: Mapped[str] = mapped_column(Text)
    computed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class Outcome(Base):
    """Итоговый исход истории — основа рейтинга. Только дополняется (триггер в миграции)."""

    __tablename__ = "outcomes"
    __table_args__ = (enum_check("verdict", Verdict), enum_check("decided_by", DecidedBy))

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    story_id: Mapped[int] = mapped_column(ForeignKey("stories.id"), index=True)
    verdict: Mapped[str] = mapped_column(String(16))
    decided_by: Mapped[str] = mapped_column(String(16))
    evidence_url: Mapped[str | None] = mapped_column(Text)
    decided_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
