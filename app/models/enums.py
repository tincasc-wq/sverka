from enum import StrEnum


class SourceType(StrEnum):
    MEDIA = "media"
    TG = "tg"
    VK = "vk"
    OFFICIAL = "official"
    AGENCY = "agency"
    # Сатира и пародия (сами так себя называют): их «новости» — вымысел по замыслу.
    # Не подтверждение; если сатира — первоисточник, новость помечается как вероятная шутка.
    SATIRE = "satire"
    # Агрегаторы пересказывают чужие публикации (Рамблер, Новости Mail, Yahoo News, каналы-
    # агрегаторы). Не независимое подтверждение и не первоисточник: если агрегатор оказался
    # раньше всех в архиве, исходная публикация, скорее всего, есть где-то ещё.
    AGGREGATOR = "aggregator"
    # Каналы мессенджера Max. Ленты без входа нет — посты попадают в архив только по ссылке
    # из проверки; запись в реестре нужна, чтобы у поста был рейтинг и владелец.
    MAX = "max"


class ChangeType(StrEnum):
    EDIT = "edit"
    DELETED = "deleted"


class StoryStatus(StrEnum):
    OPEN = "open"
    CLOSED = "closed"


class DerivationKind(StrEnum):
    EXPLICIT_LINK = "explicit_link"
    ATTRIBUTION = "attribution"
    NEAR_COPY = "near_copy"


class InputType(StrEnum):
    URL = "url"
    TG_FORWARD = "tg_forward"
    TEXT = "text"


class CheckStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"


class Stance(StrEnum):
    SUPPORTS = "supports"
    CONTRADICTS = "contradicts"
    # Поправка: публикация прямо говорит, что прежние сообщения ошибочны, и как на самом деле.
    CORRECTS = "corrects"
    REFINES = "refines"
    UNRELATED = "unrelated"


class Verdict(StrEnum):
    CONFIRMED = "confirmed"
    REFUTED = "refuted"
    MIXED = "mixed"
    UNKNOWN = "unknown"


class DecidedBy(StrEnum):
    ANALYST = "analyst"
    OFFICIAL = "official"
    AUTO = "auto"


class OrgRole(StrEnum):
    OWNER = "owner"
    MEMBER = "member"


class UserRole(StrEnum):
    USER = "user"
    ANALYST = "analyst"
    ADMIN = "admin"


class FeedbackStatus(StrEnum):
    OPEN = "open"
    RESOLVED = "resolved"
    REJECTED = "rejected"
