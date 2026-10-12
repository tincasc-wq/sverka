import os

# Тесты работают с отдельной базой sverka_test. Переменные выставляются до импорта приложения.
os.environ["DATABASE_URL"] = os.environ.get(
    "TEST_DATABASE_URL", "postgresql+asyncpg://sverka:sverka-dev@localhost:5432/sverka_test"
)
os.environ["TESTING"] = "true"

from collections.abc import AsyncIterator  # noqa: E402

import pytest  # noqa: E402
from alembic import command  # noqa: E402
from alembic.config import Config  # noqa: E402
from sqlalchemy.ext.asyncio import AsyncSession  # noqa: E402

from app.core.db import engine  # noqa: E402


@pytest.fixture(scope="session", autouse=True)
def migrated_db() -> None:
    """Пересоздаёт схему с нуля: заодно проверяет, что downgrade тоже работает."""
    cfg = Config("alembic.ini")
    cfg.attributes["database_url"] = os.environ["DATABASE_URL"]
    command.downgrade(cfg, "base")
    command.upgrade(cfg, "head")


@pytest.fixture
async def session() -> AsyncIterator[AsyncSession]:
    """Сессия внутри транзакции, которая откатывается после теста."""
    async with engine.connect() as conn:
        trans = await conn.begin()
        async with AsyncSession(bind=conn, expire_on_commit=False) as s:
            yield s
        # После ошибки БД сессия уже сама откатила транзакцию.
        if trans.is_active:
            await trans.rollback()
