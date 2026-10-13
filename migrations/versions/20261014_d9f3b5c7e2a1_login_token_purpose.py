"""login_tokens: purpose and user_id (linking email to an account)

Revision ID: d9f3b5c7e2a1
Revises: c4e8a2b6d1f0
Create Date: 2026-10-14 12:00:00
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = 'd9f3b5c7e2a1'
down_revision: str | None = 'c4e8a2b6d1f0'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        'login_tokens', sa.Column('purpose', sa.String(16), server_default='login', nullable=False)
    )
    op.add_column(
        'login_tokens', sa.Column('user_id', sa.BigInteger(), sa.ForeignKey('users.id'))
    )


def downgrade() -> None:
    op.drop_column('login_tokens', 'user_id')
    op.drop_column('login_tokens', 'purpose')
