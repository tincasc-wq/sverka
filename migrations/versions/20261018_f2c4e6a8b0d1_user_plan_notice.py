"""плашка о смене тарифа: users.plan_notice

Revision ID: f2c4e6a8b0d1
Revises: e1b3d5f7a9c0
Create Date: 2026-10-18 12:00:00
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = 'f2c4e6a8b0d1'
down_revision: str | None = 'e1b3d5f7a9c0'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column('users', sa.Column('plan_notice', sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column('users', 'plan_notice')
