from sqlalchemy import BigInteger, CheckConstraint, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, CreatedAtMixin, enum_check
from app.models.enums import FeedbackStatus, UserRole


class User(CreatedAtMixin, Base):
    __tablename__ = "users"
    __table_args__ = (enum_check("role", UserRole),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    tg_id: Mapped[int | None] = mapped_column(BigInteger, unique=True)
    role: Mapped[str] = mapped_column(String(16), server_default=UserRole.USER.value)
    plan: Mapped[str] = mapped_column(String(32), server_default="free")
    daily_quota: Mapped[int] = mapped_column(Integer, server_default="20")


class Feedback(CreatedAtMixin, Base):
    """Жалобы пользователей и апелляции источников."""

    __tablename__ = "feedback"
    __table_args__ = (
        enum_check("status", FeedbackStatus),
        CheckConstraint("check_id IS NOT NULL OR source_id IS NOT NULL", name="target_present"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    check_id: Mapped[int | None] = mapped_column(ForeignKey("checks.id"))
    source_id: Mapped[int | None] = mapped_column(ForeignKey("sources.id"))
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    text: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(16), server_default=FeedbackStatus.OPEN.value)
    resolution: Mapped[str | None] = mapped_column(Text)
