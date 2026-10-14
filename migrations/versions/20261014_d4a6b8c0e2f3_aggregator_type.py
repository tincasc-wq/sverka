"""source type aggregator

Revision ID: d4a6b8c0e2f3
Revises: c3f5a7e9b1d2
Create Date: 2026-10-14 23:30:00
"""
from collections.abc import Sequence

from alembic import op

revision: str = 'd4a6b8c0e2f3'
down_revision: str | None = 'c3f5a7e9b1d2'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

OLD = "type IN ('media', 'tg', 'vk', 'official', 'agency', 'satire')"
NEW = "type IN ('media', 'tg', 'vk', 'official', 'agency', 'satire', 'aggregator')"


def upgrade() -> None:
    op.drop_constraint(op.f('ck_sources_type'), 'sources', type_='check')
    op.create_check_constraint(op.f('ck_sources_type'), 'sources', NEW)


def downgrade() -> None:
    op.execute("UPDATE sources SET type = 'media' WHERE type = 'aggregator'")
    op.drop_constraint(op.f('ck_sources_type'), 'sources', type_='check')
    op.create_check_constraint(op.f('ck_sources_type'), 'sources', OLD)
