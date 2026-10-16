"""общие слежения организации: watches.org_id, watch_subscribers

Revision ID: b8e0a2c4d6f7
Revises: a7d9f1b3c5e6
Create Date: 2026-10-16 14:00:00
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = 'b8e0a2c4d6f7'
down_revision: str | None = 'a7d9f1b3c5e6'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column('watches', sa.Column('org_id', sa.BigInteger(), nullable=True))
    op.create_foreign_key(
        op.f('fk_watches_org_id_organizations'), 'watches', 'organizations', ['org_id'], ['id']
    )
    op.create_index(op.f('ix_watches_org_id'), 'watches', ['org_id'])
    op.create_table(
        'watch_subscribers',
        sa.Column('id', sa.BigInteger(), nullable=False),
        sa.Column('watch_id', sa.BigInteger(), nullable=False),
        sa.Column('user_id', sa.BigInteger(), nullable=False),
        sa.Column(
            'created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False
        ),
        sa.ForeignKeyConstraint(
            ['watch_id'], ['watches.id'], name=op.f('fk_watch_subscribers_watch_id_watches')
        ),
        sa.ForeignKeyConstraint(
            ['user_id'], ['users.id'], name=op.f('fk_watch_subscribers_user_id_users')
        ),
        sa.PrimaryKeyConstraint('id', name=op.f('pk_watch_subscribers')),
        sa.UniqueConstraint('watch_id', 'user_id', name=op.f('uq_watch_subscribers_watch_id')),
    )
    op.create_index(op.f('ix_watch_subscribers_watch_id'), 'watch_subscribers', ['watch_id'])


def downgrade() -> None:
    op.drop_table('watch_subscribers')
    op.drop_index(op.f('ix_watches_org_id'), table_name='watches')
    op.drop_constraint(op.f('fk_watches_org_id_organizations'), 'watches', type_='foreignkey')
    op.drop_column('watches', 'org_id')
