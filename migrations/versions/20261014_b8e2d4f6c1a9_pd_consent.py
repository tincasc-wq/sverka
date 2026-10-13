"""users.pd_consent_at

Revision ID: b8e2d4f6c1a9
Revises: a6c9e1f3b7d8
Create Date: 2026-10-14 20:00:00
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = 'b8e2d4f6c1a9'
down_revision: str | None = 'a6c9e1f3b7d8'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column('users', sa.Column('pd_consent_at', sa.DateTime(timezone=True)))


def downgrade() -> None:
    op.drop_column('users', 'pd_consent_at')
