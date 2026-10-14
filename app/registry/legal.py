"""Правовые пометки источников в выдаче (legal_flags в реестре).

Пометка — факт о статусе источника в РФ, на рейтинг она не влияет. Порядок — от более
строгого статуса к менее строгому; показывается одна, самая строгая.
"""

LABELS = {
    "undesirable": "нежелательная организация в РФ",
    "foreign_agent": "иностранный агент",
    "blocked_in_ru": "заблокировано в РФ",
}


def legal_note(flags: dict | None) -> str | None:
    for key, label in LABELS.items():
        if (flags or {}).get(key):
            return label
    return None


SATIRE_NOTE = "сатирическое издание"
AGGREGATOR_NOTE = "агрегатор"


def source_note(source_type: str | None, flags: dict | None) -> str | None:
    """Пометка у имени источника: сатира важнее правового статуса — она меняет смысл вывода."""
    if source_type == "satire":
        return SATIRE_NOTE
    legal = legal_note(flags)
    if source_type == "aggregator":
        return f"{AGGREGATOR_NOTE}, {legal}" if legal else AGGREGATOR_NOTE
    return legal


def display_name(name: str, note: str | None) -> str:
    return f"{name} ({note})" if note else name
