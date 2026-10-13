"""watches — повторная проверка по расписанию

Revision ID: c3f5a7e9b1d2
Revises: b8e2d4f6c1a9
Create Date: 2026-10-14 23:00:00
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = 'c3f5a7e9b1d2'
down_revision: str | None = 'b8e2d4f6c1a9'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        'watches',
        sa.Column('id', sa.BigInteger(), nullable=False),
        sa.Column('user_id', sa.BigInteger(), nullable=False),
        sa.Column('check_id', sa.BigInteger(), nullable=False),
        sa.Column('last_check_id', sa.BigInteger(), nullable=True),
        sa.Column('every_hours', sa.Integer(), nullable=False),
        sa.Column('next_run_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('until', sa.DateTime(timezone=True), nullable=False),
        sa.Column('runs', sa.Integer(), server_default='0', nullable=False),
        sa.Column('active', sa.Boolean(), server_default='true', nullable=False),
        sa.Column('first_state', postgresql.JSONB(), server_default='{}', nullable=False),
        sa.Column('last_state', postgresql.JSONB(), server_default='{}', nullable=False),
        sa.Column('stopped_reason', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], name=op.f('fk_watches_user_id_users')),
        sa.ForeignKeyConstraint(['check_id'], ['checks.id'], name=op.f('fk_watches_check_id_checks')),
        sa.ForeignKeyConstraint(['last_check_id'], ['checks.id'], name=op.f('fk_watches_last_check_id_checks')),
        sa.PrimaryKeyConstraint('id', name=op.f('pk_watches')),
    )
    op.create_index(op.f('ix_watches_user_id'), 'watches', ['user_id'])
    op.create_index(op.f('ix_watches_next_run_at'), 'watches', ['next_run_at'])


def downgrade() -> None:
    op.drop_table('watches')
