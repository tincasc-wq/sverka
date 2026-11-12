"""GIN trigram-индекс по documents.text: поиск по редким именам шёл полным сканированием

Revision ID: c4d2e8f1a3b5
Revises: b3f1a7c9d2e4
Create Date: 2026-11-12 23:30:00

Поиск публикаций по редкому имени (другими словами, поправка, ирония) — ILIKE '%имя%'
по text и title. По title индекс trgm уже был, а по text каждый такой запрос шёл
Parallel Seq Scan по 617 тыс. документов — 77 с на имя (замер 12.11.2026), сборка
истории проверки растягивалась на десятки секунд. CONCURRENTLY — не блокировать сборщики;
IF NOT EXISTS — идемпотентно (на проде поднят вручную заранее).
"""
from collections.abc import Sequence

from alembic import op

revision: str = 'c4d2e8f1a3b5'
down_revision: str | None = 'b3f1a7c9d2e4'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute('COMMIT')  # CREATE INDEX CONCURRENTLY не работает внутри транзакции
    op.execute(
        'CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_documents_text_trgm '
        'ON documents USING gin (text gin_trgm_ops)'
    )


def downgrade() -> None:
    op.execute('COMMIT')
    op.execute('DROP INDEX CONCURRENTLY IF EXISTS ix_documents_text_trgm')
