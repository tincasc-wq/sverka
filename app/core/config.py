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
    # Без прокси ходить к t.me только по IPv6 (сервер в РФ: по IPv4 t.me не отвечает).
    telegram_ipv6: bool = False
    # Шлюз для RSS-источников с via_gateway; пусто — тот же прокси, что для Telegram.
    collector_proxy: str = ""

    # Эмбеддинги
    embedding_model: str = "BAAI/bge-m3"
    embedding_batch_size: int = 32
    # 256 токенов (заголовок и начало текста): в 1,75 раза быстрее 512, близость сдвигается
    # в среднем на 0,014 — замер на 30 парах публикаций, 15.10.2026. 128 — уже до 0,22.
    embedding_max_chars: int = 1200
    embedding_max_tokens: int = 256
    # Из 4 ядер сервера. Днём приходит до 1 300 публикаций в час — на 2 ядрах модель шла
    # вровень с притоком и при любой задержке отставала навсегда (15.10.2026).
    embedding_threads: int = 3

    # Вход в админку (временная схема до Telegram Login). Пусто — вход закрыт.
    admin_token: str = ""

    # Сайт (этап 6): подпись cookie посетителя. Пусто — случайный ключ на время работы процесса
    # (после перезапуска посетители «теряют» историю); на сервере задать обязательно.
    secret_key: str = ""
    # Лимиты проверок — по тарифам, config/plans.yaml. Платные тарифы пока подключаются
    # вручную: куда писать — e-mail или @telegram на странице «Тарифы».
    sales_contact: str = ""
    # Telegram ID владельца: сюда бот присылает заявки на тарифы и служебные оповещения.
    admin_tg_id: int = 0

    # Почта для входа по одноразовой ссылке. Пусто — письма не отправляются, ссылка пишется
    # в лог приложения (только для локальной разработки).
    smtp_host: str = ""
    smtp_port: int = 465  # 465 — SSL; 587 — STARTTLS
    smtp_user: str = ""
    smtp_password: str = ""
    smtp_from: str = ""  # например: Сверка <noreply@ang-rist.ru>
    search_days: int = 30  # окно поиска по архиву на сайте
    # Имя бота без @ для кнопки «Войти через Telegram»; домен сайта — у @BotFather (/setdomain).
    telegram_bot_username: str = ""

    # Telegram-бот (этап 4). Сеть до api.telegram.org — через telegram_proxy, как и сбор каналов.
    telegram_bot_token: str = ""
    # Адрес, по которому пользователь открывает веб-отчёт /r/<token>.
    public_base_url: str = "http://localhost:8000"
    check_cache_hours: float = 6  # ФТ-1.3
    recheck_hours: float = 6  # кнопка «Проверить снова позже» (ФТ-7.4)
    text_search_days: int = 7  # окно поиска для произвольного текста (сценарий 3)

    # LLM (раздел 8.1): по умолчанию DeepSeek; к зарубежным провайдерам — через шлюз LLM_PROXY.
    llm_proxy: str = ""
    deepseek_api_key: str = ""
    qwen_api_key: str = ""
    yandex_ai_studio_api_key: str = ""
    yandex_folder_id: str = ""  # каталог Yandex Cloud: модель задаётся как gpt://<каталог>/<модель>
    # GigaChat (Сбер): ключ авторизации (Basic) меняется на токен доступа на 30 минут.
    gigachat_auth_key: str = ""
    gigachat_scope: str = "GIGACHAT_API_PERS"  # для юрлиц — GIGACHAT_API_CORP
    # Сертификат «Russian Trusted Root CA» (НУЦ Минцифры): без него TLS к GigaChat не проходит.
    gigachat_ca_bundle: str = ""
    # Запасной вариант, выключен в config/llm.yaml — только при законном доступе к API.
    anthropic_api_key: str = ""


@lru_cache
def get_settings() -> Settings:
    return Settings()
