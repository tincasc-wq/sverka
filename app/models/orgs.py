"""Организации и места (ФТ-9.2): корпоративные клиенты — редакции, аппараты, PR-службы."""

from datetime import datetime

from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, CreatedAtMixin, enum_check
from app.models.enums import OrgRole


class Organization(CreatedAtMixin, Base):
    __tablename__ = "organizations"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    name: Mapped[str] = mapped_column(String(255))
    plan: Mapped[str] = mapped_column(String(32), server_default="editorial")
    seats: Mapped[int] = mapped_column(Integer, server_default="5")
    monthly_quota: Mapped[int] = mapped_column(Integer, server_default="3000")
    # Госконтур: запросы только в российские модели (раздел 12.5, аппараты депутатов).
    ru_only: Mapped[bool] = mapped_column(Boolean, server_default="false", default=False)
    # Оплата у юрлиц — по счёту вручную (ФТ-9.5): в системе только статус и срок.
    active: Mapped[bool] = mapped_column(Boolean, server_default="true", default=True)
    # До какого дня оплачено (вносит администратор); владелец видит срок в кабинете.
    paid_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class OrgMember(CreatedAtMixin, Base):
    __tablename__ = "org_members"
    __table_args__ = (enum_check("role", OrgRole),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    org_id: Mapped[int] = mapped_column(ForeignKey("organizations.id"), index=True)
    # Один пользователь — одна организация: чьей квотой платить и чей контур, однозначно.
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), unique=True)
    role: Mapped[str] = mapped_column(String(16), server_default=OrgRole.MEMBER.value)


class OrgInvite(CreatedAtMixin, Base):
    """Ссылка-приглашение /join/<token>: одноразовая, со сроком действия."""

    __tablename__ = "org_invites"
    __table_args__ = (enum_check("role", OrgRole),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    org_id: Mapped[int] = mapped_column(ForeignKey("organizations.id"), index=True)
    token: Mapped[str] = mapped_column(String(32), unique=True)
    role: Mapped[str] = mapped_column(String(16), server_default=OrgRole.MEMBER.value)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    used_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ApiKey(CreatedAtMixin, Base):
    """Ключ API организации. Храним только SHA-256: сам ключ показываем один раз при выпуске.

    Проверки по ключу идут от имени сотрудника, который его выпустил, и в квоту организации.
    """

    __tablename__ = "api_keys"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    org_id: Mapped[int] = mapped_column(ForeignKey("organizations.id"), index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    name: Mapped[str] = mapped_column(String(64))
    prefix: Mapped[str] = mapped_column(String(16))  # начало ключа — чтобы узнать его в списке
    key_hash: Mapped[str] = mapped_column(String(64), unique=True)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
