"""plan_requests: requests from the «Подключить» button

Revision ID: a6c9e1f3b7d8
Revises: f2b8d4e6a3c5
Create Date: 2026-10-14 18:00:00
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = 'a6c9e1f3b7d8'
down_revision: str | None = 'f2b8d4e6a3c5'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        'plan_requests',
        sa.Column('id', sa.BigInteger(), primary_key=True),
        sa.Column('user_id', sa.BigInteger(), sa.ForeignKey('users.id')),
        sa.Column('plan', sa.String(32), nullable=False),
        sa.Column('contact', sa.String(255), nullable=False),
        sa.Column('org_name', sa.String(255)),
        sa.Column('comment', sa.Text()),
        sa.Column('ip', sa.String(64)),
        sa.Column('status', sa.String(16), server_default='new', nullable=False),
        sa.Column('notified', sa.Boolean(), server_default='false', nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index('ix_plan_requests_ip', 'plan_requests', ['ip'])


def downgrade() -> None:
    op.drop_table('plan_requests')
