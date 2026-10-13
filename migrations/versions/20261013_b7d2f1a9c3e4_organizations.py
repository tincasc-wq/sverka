"""organizations, members, invites; checks.org_id

Revision ID: b7d2f1a9c3e4
Revises: a3c1e9f04b72
Create Date: 2026-10-13 05:00:00
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = 'b7d2f1a9c3e4'
down_revision: str | None = 'a3c1e9f04b72'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ROLE_CHECK = "role IN ('owner', 'member')"


def upgrade() -> None:
    op.create_table(
        'organizations',
        sa.Column('id', sa.BigInteger(), primary_key=True),
        sa.Column('name', sa.String(255), nullable=False),
        sa.Column('plan', sa.String(32), server_default='editorial', nullable=False),
        sa.Column('seats', sa.Integer(), server_default='5', nullable=False),
        sa.Column('monthly_quota', sa.Integer(), server_default='3000', nullable=False),
        sa.Column('ru_only', sa.Boolean(), server_default='false', nullable=False),
        sa.Column('active', sa.Boolean(), server_default='true', nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_table(
        'org_members',
        sa.Column('id', sa.BigInteger(), primary_key=True),
        sa.Column('org_id', sa.BigInteger(), sa.ForeignKey('organizations.id'), nullable=False),
        sa.Column('user_id', sa.BigInteger(), sa.ForeignKey('users.id'), nullable=False),
        sa.Column('role', sa.String(16), server_default='member', nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint('user_id', name='uq_org_members_user_id'),
        sa.CheckConstraint(ROLE_CHECK, name='ck_org_members_role'),
    )
    op.create_index('ix_org_members_org_id', 'org_members', ['org_id'])
    op.create_table(
        'org_invites',
        sa.Column('id', sa.BigInteger(), primary_key=True),
        sa.Column('org_id', sa.BigInteger(), sa.ForeignKey('organizations.id'), nullable=False),
        sa.Column('token', sa.String(32), nullable=False),
        sa.Column('role', sa.String(16), server_default='member', nullable=False),
        sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('used_by', sa.BigInteger(), sa.ForeignKey('users.id')),
        sa.Column('used_at', sa.DateTime(timezone=True)),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint('token', name='uq_org_invites_token'),
        sa.CheckConstraint(ROLE_CHECK, name='ck_org_invites_role'),
    )
    op.create_index('ix_org_invites_org_id', 'org_invites', ['org_id'])
    op.add_column('checks', sa.Column('org_id', sa.BigInteger(), sa.ForeignKey('organizations.id')))
    op.create_index('ix_checks_org_id', 'checks', ['org_id'])


def downgrade() -> None:
    op.drop_index('ix_checks_org_id', 'checks')
    op.drop_column('checks', 'org_id')
    op.drop_table('org_invites')
    op.drop_table('org_members')
    op.drop_table('organizations')
