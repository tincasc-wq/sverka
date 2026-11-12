"""HNSW-индекс по эмбеддингам на проде: векторный поиск шёл полным сканированием

Revision ID: b3f1a7c9d2e4
Revises: f8c0d2e4a6b7
Create Date: 2026-11-12 15:30:00

На проде индекса не было (база переносилась pg_dump до его появления в модели):
каждый поиск ближайших шёл Parallel Seq Scan по 617 тыс. документов — 7–37 с на
запрос, история собиралась ~38 с (замер 12.11.2026). CONCURRENTLY — не блокировать
сборщики; IF NOT EXISTS — идемпотентно, если индекс уже создан вручную.
"""
from collections.abc import Sequence

from alembic import op

revision: str = 'b3f1a7c9d2e4'
down_revision: str | None = 'f8c0d2e4a6b7'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute('COMMIT')  # CREATE INDEX CONCURRENTLY не работает внутри транзакции
    op.execute(
        'CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_documents_embedding_hnsw '
        'ON documents USING hnsw (embedding vector_cosine_ops)'
    )


def downgrade() -> None:
    op.execute('COMMIT')
    op.execute('DROP INDEX CONCURRENTLY IF EXISTS ix_documents_embedding_hnsw')
