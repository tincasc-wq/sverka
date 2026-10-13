from datetime import datetime

from sqlalchemy import BigInteger, CheckConstraint, DateTime, ForeignKey, Integer, String, Text
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
    # Вход по почте (одноразовая ссылка): адрес в нижнем регистре, подтверждён ссылкой.
    email: Mapped[str | None] = mapped_column(String(255), unique=True)
    email_verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class LoginToken(CreatedAtMixin, Base):
    """Одноразовая ссылка входа по почте. Храним только хэш: утечка базы не даёт войти."""

    __tablename__ = "login_tokens"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    email: Mapped[str] = mapped_column(String(255), index=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    ip: Mapped[str | None] = mapped_column(String(64), index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # login — войти; link — привязать адрес к учётной записи user_id (страница «Учётная запись»).
    purpose: Mapped[str] = mapped_column(String(16), server_default="login", default="login")
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))


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
