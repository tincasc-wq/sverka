"""source type satire; checks.channel

Revision ID: e1a7c3d5f9b2
Revises: d9f3b5c7e2a1
Create Date: 2026-10-14 14:00:00
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = 'e1a7c3d5f9b2'
down_revision: str | None = 'd9f3b5c7e2a1'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_constraint(op.f('ck_sources_type'), 'sources', type_='check')
    op.create_check_constraint(
        op.f('ck_sources_type'), 'sources', "type IN ('media', 'tg', 'vk', 'official', 'agency', 'satire')"
    )
    op.add_column('checks', sa.Column('channel', sa.String(16)))
    # Восстановить канал для старых проверок по косвенным признакам.
    op.execute("UPDATE checks SET channel = 'service' WHERE user_id IS NULL")
    op.execute("UPDATE checks SET channel = 'bot' WHERE channel IS NULL AND input_type = 'tg_forward'")
    op.execute(
        "UPDATE checks c SET channel = 'web' FROM users u "
        "WHERE c.channel IS NULL AND u.id = c.user_id AND u.tg_id IS NULL"
    )
    op.execute("UPDATE checks SET channel = 'bot' WHERE channel IS NULL")


def downgrade() -> None:
    op.drop_column('checks', 'channel')
    op.drop_constraint(op.f('ck_sources_type'), 'sources', type_='check')
    op.create_check_constraint(
        op.f('ck_sources_type'), 'sources', "type IN ('media', 'tg', 'vk', 'official', 'agency')"
    )
