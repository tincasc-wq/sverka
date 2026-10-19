"""отметка о запрете в robots.txt: sources.robots_note

Revision ID: c5f7a9b1d3e4
Revises: b4e6a8c0d2f3
Create Date: 2026-10-20 10:00:00
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = 'c5f7a9b1d3e4'
down_revision: str | None = 'b4e6a8c0d2f3'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column('sources', sa.Column('robots_note', sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column('sources', 'robots_note')
