"""редкий опрос источника: sources.poll_minutes

Revision ID: f8c0d2e4a6b7
Revises: e7b9c1d3f5a6
Create Date: 2026-10-20 13:00:00
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = 'f8c0d2e4a6b7'
down_revision: str | None = 'e7b9c1d3f5a6'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column('sources', sa.Column('poll_minutes', sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column('sources', 'poll_minutes')
