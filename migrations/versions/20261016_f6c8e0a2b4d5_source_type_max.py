"""source type max

Revision ID: f6c8e0a2b4d5
Revises: e5b7c9d1f3a4
Create Date: 2026-10-16 03:00:00
"""
from collections.abc import Sequence

from alembic import op

revision: str = 'f6c8e0a2b4d5'
down_revision: str | None = 'e5b7c9d1f3a4'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

OLD = "type IN ('media', 'tg', 'vk', 'official', 'agency', 'satire', 'aggregator')"
NEW = "type IN ('media', 'tg', 'vk', 'official', 'agency', 'satire', 'aggregator', 'max')"


def upgrade() -> None:
    op.drop_constraint(op.f('ck_sources_type'), 'sources', type_='check')
    op.create_check_constraint(op.f('ck_sources_type'), 'sources', NEW)


def downgrade() -> None:
    op.execute("UPDATE sources SET type = 'tg' WHERE type = 'max'")
    op.drop_constraint(op.f('ck_sources_type'), 'sources', type_='check')
    op.create_check_constraint(op.f('ck_sources_type'), 'sources', OLD)
