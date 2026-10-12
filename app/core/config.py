from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql+asyncpg://sverka:sverka-dev@localhost:5432/sverka"
    redis_url: str = "redis://localhost:6379/0"
    # В тестах пул соединений отключается: каждый тест живёт в своём event loop.
    testing: bool = False

    anthropic_api_key: str = ""
    deepseek_api_key: str = ""
    yandex_ai_studio_api_key: str = ""


@lru_cache
def get_settings() -> Settings:
    return Settings()
