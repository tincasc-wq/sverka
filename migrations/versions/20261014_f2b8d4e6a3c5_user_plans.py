"""user plans: plan default standard, plan_until, daily_quota as optional override

Revision ID: f2b8d4e6a3c5
Revises: e1a7c3d5f9b2
Create Date: 2026-10-14 16:00:00
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = 'f2b8d4e6a3c5'
down_revision: str | None = 'e1a7c3d5f9b2'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column('users', sa.Column('plan_until', sa.DateTime(timezone=True)))
    op.alter_column('users', 'plan', server_default='standard')
    op.execute("UPDATE users SET plan = 'standard' WHERE plan = 'free'")
    # daily_quota — теперь только личная поправка админа; обычный лимит берётся из тарифа.
    op.alter_column('users', 'daily_quota', nullable=True, server_default=None)
    op.execute("UPDATE users SET daily_quota = NULL")


def downgrade() -> None:
    op.execute("UPDATE users SET daily_quota = 20 WHERE daily_quota IS NULL")
    op.alter_column('users', 'daily_quota', nullable=False, server_default='20')
    op.alter_column('users', 'plan', server_default='free')
    op.drop_column('users', 'plan_until')
