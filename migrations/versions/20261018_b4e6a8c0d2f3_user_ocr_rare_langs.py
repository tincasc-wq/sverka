"""настройка «нестандартные языки» распознавания: users.ocr_rare_langs

Revision ID: b4e6a8c0d2f3
Revises: a3d5f7b9c1e2
Create Date: 2026-10-18 21:00:00
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = 'b4e6a8c0d2f3'
down_revision: str | None = 'a3d5f7b9c1e2'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        'users',
        sa.Column('ocr_rare_langs', sa.Boolean(), server_default=sa.text('false'), nullable=False),
    )


def downgrade() -> None:
    op.drop_column('users', 'ocr_rare_langs')
