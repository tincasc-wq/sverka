"""source lang and via_gateway

Revision ID: a3c1e9f04b72
Revises: 864331f11924
Create Date: 2026-10-13 00:10:00
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = 'a3c1e9f04b72'
down_revision: str | None = '864331f11924'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column('sources', sa.Column('lang', sa.String(length=8), server_default='ru', nullable=False))
    op.add_column(
        'sources', sa.Column('via_gateway', sa.Boolean(), server_default='false', nullable=False)
    )


def downgrade() -> None:
    op.drop_column('sources', 'via_gateway')
    op.drop_column('sources', 'lang')
