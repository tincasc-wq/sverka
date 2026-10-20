"""главная находка или упоминание: topic_hits.prominent

Revision ID: e7b9c1d3f5a6
Revises: d6a8b0c2e4f5
Create Date: 2026-10-20 12:00:00
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = 'e7b9c1d3f5a6'
down_revision: str | None = 'd6a8b0c2e4f5'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        'topic_hits',
        sa.Column('prominent', sa.Boolean(), server_default=sa.text('true'), nullable=False),
    )


def downgrade() -> None:
    op.drop_column('topic_hits', 'prominent')
