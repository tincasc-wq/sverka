"""organizations.paid_until

Revision ID: a7d9f1b3c5e6
Revises: f6c8e0a2b4d5
Create Date: 2026-10-16 12:00:00
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = 'a7d9f1b3c5e6'
down_revision: str | None = 'f6c8e0a2b4d5'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column('organizations', sa.Column('paid_until', sa.DateTime(timezone=True)))


def downgrade() -> None:
    op.drop_column('organizations', 'paid_until')
