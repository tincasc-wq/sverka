"""настройка «распознать текст на картинках поста»: users.ocr_images

Revision ID: a3d5f7b9c1e2
Revises: f2c4e6a8b0d1
Create Date: 2026-10-18 18:00:00
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = 'a3d5f7b9c1e2'
down_revision: str | None = 'f2c4e6a8b0d1'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        'users',
        sa.Column('ocr_images', sa.Boolean(), server_default=sa.text('false'), nullable=False),
    )


def downgrade() -> None:
    op.drop_column('users', 'ocr_images')
