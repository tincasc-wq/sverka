"""поправки: позиция corrects, sourcing у сверки, отслеживание поправок у проверок

Revision ID: e5b7c9d1f3a4
Revises: d4a6b8c0e2f3
Create Date: 2026-10-14 18:00:00
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = 'e5b7c9d1f3a4'
down_revision: str | None = 'd4a6b8c0e2f3'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

OLD = "stance IN ('supports', 'contradicts', 'refines', 'unrelated')"
NEW = "stance IN ('supports', 'contradicts', 'corrects', 'refines', 'unrelated')"


def upgrade() -> None:
    op.drop_constraint(op.f('ck_claim_evidence_stance'), 'claim_evidence', type_='check')
    op.create_check_constraint(op.f('ck_claim_evidence_stance'), 'claim_evidence', NEW)
    op.add_column('claim_evidence', sa.Column('sourcing', sa.String(16)))
    op.add_column('checks', sa.Column('auto_checked_at', sa.DateTime(timezone=True)))
    op.add_column('checks', sa.Column('auto_runs', sa.Integer(), server_default='0', nullable=False))


def downgrade() -> None:
    op.drop_column('checks', 'auto_runs')
    op.drop_column('checks', 'auto_checked_at')
    op.drop_column('claim_evidence', 'sourcing')
    op.execute("UPDATE claim_evidence SET stance = 'contradicts' WHERE stance = 'corrects'")
    op.drop_constraint(op.f('ck_claim_evidence_stance'), 'claim_evidence', type_='check')
    op.create_check_constraint(op.f('ck_claim_evidence_stance'), 'claim_evidence', OLD)
