"""email login: users.email, login_tokens

Revision ID: c4e8a2b6d1f0
Revises: b7d2f1a9c3e4
Create Date: 2026-10-14 10:00:00
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = 'c4e8a2b6d1f0'
down_revision: str | None = 'b7d2f1a9c3e4'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column('users', sa.Column('email', sa.String(255)))
    op.add_column('users', sa.Column('email_verified_at', sa.DateTime(timezone=True)))
    op.create_unique_constraint('uq_users_email', 'users', ['email'])
    op.create_table(
        'login_tokens',
        sa.Column('id', sa.BigInteger(), primary_key=True),
        sa.Column('email', sa.String(255), nullable=False),
        sa.Column('token_hash', sa.String(64), nullable=False),
        sa.Column('ip', sa.String(64)),
        sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('used_at', sa.DateTime(timezone=True)),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint('token_hash', name='uq_login_tokens_token_hash'),
    )
    op.create_index('ix_login_tokens_email', 'login_tokens', ['email'])
    op.create_index('ix_login_tokens_ip', 'login_tokens', ['ip'])


def downgrade() -> None:
    op.drop_table('login_tokens')
    op.drop_constraint('uq_users_email', 'users', type_='unique')
    op.drop_column('users', 'email_verified_at')
    op.drop_column('users', 'email')
