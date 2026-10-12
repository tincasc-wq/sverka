from datetime import datetime

from sqlalchemy import BigInteger, Boolean, DateTime, Float, ForeignKey, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, CreatedAtMixin, enum_check
from app.models.enums import CheckStatus, InputType, Stance


class Check(CreatedAtMixin, Base):
    """Запрос пользователя на проверку новости."""

    __tablename__ = "checks"
    __table_args__ = (enum_check("input_type", InputType), enum_check("status", CheckStatus))

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), index=True)
    input_type: Mapped[str] = mapped_column(String(16))
    input_raw: Mapped[str] = mapped_column(Text)
    document_id: Mapped[int | None] = mapped_column(ForeignKey("documents.id"))
    story_id: Mapped[int | None] = mapped_column(ForeignKey("stories.id"))
    status: Mapped[str] = mapped_column(String(16), server_default=CheckStatus.QUEUED.value)
    confirmation_index: Mapped[float | None] = mapped_column(Float)
    summary: Mapped[str | None] = mapped_column(Text)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Claim(Base):
    """Проверяемое утверждение, извлечённое из новости (ФТ-2.1)."""

    __tablename__ = "claims"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    check_id: Mapped[int] = mapped_column(ForeignKey("checks.id"), index=True)
    text: Mapped[str] = mapped_column(Text)
    structured: Mapped[dict] = mapped_column(JSONB, server_default="{}", default=dict)
    is_checkable: Mapped[bool] = mapped_column(Boolean, server_default="true", default=True)


class ClaimEvidence(Base):
    """Результат сверки утверждения с публикацией (ФТ-5.1)."""

    __tablename__ = "claim_evidence"
    __table_args__ = (enum_check("stance", Stance),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    claim_id: Mapped[int] = mapped_column(ForeignKey("claims.id"), index=True)
    document_id: Mapped[int] = mapped_column(ForeignKey("documents.id"), index=True)
    stance: Mapped[str] = mapped_column(String(16))
    quote: Mapped[str | None] = mapped_column(Text)
    discrepancy: Mapped[dict | None] = mapped_column(JSONB)
    is_independent: Mapped[bool] = mapped_column(Boolean, server_default="false", default=False)
