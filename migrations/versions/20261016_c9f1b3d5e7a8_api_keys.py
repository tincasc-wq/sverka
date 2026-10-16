"""ключи API организаций

Revision ID: c9f1b3d5e7a8
Revises: b8e0a2c4d6f7
Create Date: 2026-10-16 18:00:00
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = 'c9f1b3d5e7a8'
down_revision: str | None = 'b8e0a2c4d6f7'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        'api_keys',
        sa.Column('id', sa.BigInteger(), nullable=False),
        sa.Column('org_id', sa.BigInteger(), nullable=False),
        sa.Column('user_id', sa.BigInteger(), nullable=False),
        sa.Column('name', sa.String(length=64), nullable=False),
        sa.Column('prefix', sa.String(length=16), nullable=False),
        sa.Column('key_hash', sa.String(length=64), nullable=False),
        sa.Column('last_used_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('revoked_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            'created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False
        ),
        sa.ForeignKeyConstraint(
            ['org_id'], ['organizations.id'], name=op.f('fk_api_keys_org_id_organizations')
        ),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], name=op.f('fk_api_keys_user_id_users')),
        sa.PrimaryKeyConstraint('id', name=op.f('pk_api_keys')),
        sa.UniqueConstraint('key_hash', name=op.f('uq_api_keys_key_hash')),
    )
    op.create_index(op.f('ix_api_keys_org_id'), 'api_keys', ['org_id'])


def downgrade() -> None:
    op.drop_table('api_keys')
