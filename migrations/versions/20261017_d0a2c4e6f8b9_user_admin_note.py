"""пометка администратора у пользователя

Revision ID: d0a2c4e6f8b9
Revises: c9f1b3d5e7a8
Create Date: 2026-10-17 12:00:00
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = 'd0a2c4e6f8b9'
down_revision: str | None = 'c9f1b3d5e7a8'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column('users', sa.Column('admin_note', sa.String(length=255), nullable=True))


def downgrade() -> None:
    op.drop_column('users', 'admin_note')
