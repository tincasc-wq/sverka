from decimal import Decimal

from sqlalchemy import BigInteger, ForeignKey, Integer, Numeric, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, CreatedAtMixin


class LlmCall(CreatedAtMixin, Base):
    """Учёт каждого вызова LLM: расходы, задержка, переходы на резерв (раздел 8.1)."""

    __tablename__ = "llm_calls"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    check_id: Mapped[int | None] = mapped_column(ForeignKey("checks.id"), index=True)
    stage: Mapped[str] = mapped_column(String(32))
    provider: Mapped[str] = mapped_column(String(32))
    model: Mapped[str] = mapped_column(String(64))
    prompt_version: Mapped[str | None] = mapped_column(String(64))
    tokens_in: Mapped[int] = mapped_column(Integer, server_default="0")
    tokens_out: Mapped[int] = mapped_column(Integer, server_default="0")
    cost_usd: Mapped[Decimal] = mapped_column(Numeric(12, 6), server_default="0")
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    # Почему ушли с основного провайдера (ошибка, таймаут, невалидный JSON); пусто — не уходили.
    fallback_reason: Mapped[str | None] = mapped_column(Text)
