"""Ключи API организаций: выпуск, отзыв, проверка.

Ключ — «sv_» и 43 случайных символа. В базе только SHA-256 (у ключа достаточно энтропии,
медленный хеш не нужен) и первые символы — чтобы владелец узнал ключ в списке.
"""

import hashlib
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import ApiKey, Organization, User
from app.orgs.service import Membership, membership_of

PREFIX = "sv_"
MAX_KEYS = 5  # активных на организацию


class KeyRefused(Exception):
    pass


def hash_key(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


async def active_keys(session: AsyncSession, org: Organization) -> list[ApiKey]:
    return list(
        await session.scalars(
            select(ApiKey)
            .where(ApiKey.org_id == org.id, ApiKey.revoked_at.is_(None))
            .order_by(ApiKey.id)
        )
    )


async def issue_key(
    session: AsyncSession, org: Organization, user: User, name: str
) -> tuple[ApiKey, str]:
    """Новый ключ; сам ключ возвращается один раз — потом его не восстановить."""
    if len(await active_keys(session, org)) >= MAX_KEYS:
        raise KeyRefused(f"У организации уже {MAX_KEYS} ключей. Отзовите ненужный.")
    raw = PREFIX + secrets.token_urlsafe(32)
    key = ApiKey(
        org_id=org.id,
        user_id=user.id,
        name=(name.strip() or "ключ")[:64],
        prefix=raw[:10],
        key_hash=hash_key(raw),
    )
    session.add(key)
    await session.commit()
    return key, raw


async def revoke_key(session: AsyncSession, org: Organization, key_id: int) -> bool:
    key = await session.get(ApiKey, key_id)
    if key is None or key.org_id != org.id or key.revoked_at is not None:
        return False
    key.revoked_at = datetime.now(UTC)
    await session.commit()
    return True


@dataclass
class ApiCaller:
    key: ApiKey
    user: User
    membership: Membership


async def authenticate(session: AsyncSession, raw: str | None) -> ApiCaller | None:
    """Ключ действует, пока не отозван и пока выпустивший его остаётся в организации."""
    if not raw or not raw.startswith(PREFIX):
        return None
    key = await session.scalar(
        select(ApiKey).where(ApiKey.key_hash == hash_key(raw), ApiKey.revoked_at.is_(None))
    )
    if key is None:
        return None
    user = await session.get(User, key.user_id)
    membership = await membership_of(session, user)
    if membership is None or membership.org.id != key.org_id:
        return None
    key.last_used_at = datetime.now(UTC)
    await session.commit()
    return ApiCaller(key, user, membership)
