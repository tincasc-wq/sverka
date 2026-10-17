"""дата публикации без времени со страницы: documents.stated_date

Revision ID: e1b3d5f7a9c0
Revises: d0a2c4e6f8b9
Create Date: 2026-10-17 20:00:00
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = 'e1b3d5f7a9c0'
down_revision: str | None = 'd0a2c4e6f8b9'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column('documents', sa.Column('stated_date', sa.Date(), nullable=True))


def downgrade() -> None:
    op.drop_column('documents', 'stated_date')
