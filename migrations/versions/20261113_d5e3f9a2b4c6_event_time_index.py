"""Btree-индекс по времени события: bitmap-AND с trigram в поиске по именам

Revision ID: d5e3f9a2b4c6
Revises: c4d2e8f1a3b5
Create Date: 2026-11-13 00:10:00

Поиск по редким именам: trigram-индекс по text даёт десятки тысяч строк, и heap
приходится читать их все (30 с, замер 12.11.2026). Оконный фильтр по времени —
по выражению coalesce(published_at, first_seen_at), оно не было проиндексировано.
С btree по этому выражению планировщик строит BitmapAnd: читаются только строки
окна. CONCURRENTLY — не блокировать сборщики; IF NOT EXISTS — идемпотентно.
"""
from collections.abc import Sequence

from alembic import op

revision: str = 'd5e3f9a2b4c6'
down_revision: str | None = 'c4d2e8f1a3b5'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute('COMMIT')  # CREATE INDEX CONCURRENTLY не работает внутри транзакции
    op.execute(
        'CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_documents_event_time '
        'ON documents (coalesce(published_at, first_seen_at))'
    )


def downgrade() -> None:
    op.execute('COMMIT')
    op.execute('DROP INDEX CONCURRENTLY IF EXISTS ix_documents_event_time')
