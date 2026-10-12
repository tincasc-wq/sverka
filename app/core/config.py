from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql+asyncpg://sverka:sverka-dev@localhost:5432/sverka"
    redis_url: str = "redis://localhost:6379/0"
    # В тестах пул соединений отключается: каждый тест живёт в своём event loop.
    testing: bool = False

    # Сборщики
    collector_concurrency: int = 10
    http_timeout_s: float = 20.0
    user_agent: str = "Mozilla/5.0 (compatible; SverkaBot/0.1)"
    fetch_full_text: bool = True
    # Прокси для t.me, если Telegram недоступен напрямую (например, http://host:port).
    telegram_proxy: str = ""

    # Эмбеддинги
    embedding_model: str = "BAAI/bge-m3"
    embedding_batch_size: int = 32
    embedding_max_chars: int = 2000
    embedding_threads: int = 4

    # Вход в админку (временная схема до Telegram Login). Пусто — вход закрыт.
    admin_token: str = ""

    anthropic_api_key: str = ""
    deepseek_api_key: str = ""
    yandex_ai_studio_api_key: str = ""


@lru_cache
def get_settings() -> Settings:
    return Settings()
