"""Собственный источник издания — распознаём по формулировке, не полагаясь только на LLM.

«Сообщили РИА Новости в силовых структурах», «рассказал ТАСС источник», «заявил „Известиям“»
в тексте самого издания (или его канала) — это его эксклюзив. Модель путала такое с
пересказом, причём от прогона к прогону по-разному (Сенной рынок 17.10.2026: индекс 69 → «не
подтверждено» → 71…85). Та же фраза в чужом тексте («Коммерсантъ» пересказывает РИА) —
пересказ: имя в ней не своё.
"""

import re
from functools import lru_cache

from app.pipeline.graph import Node, SourceInfo

_VERB = r"(?:сообщил|рассказал|заявил|сказал|подтвердил|уточнил|пояснил|отметил|признал)\w*"


@lru_cache(maxsize=2048)
def _patterns(alias: str) -> tuple[re.Pattern, ...]:
    flags = 0 if len(alias) <= 3 else re.IGNORECASE
    name = rf"[«\"„]?{re.escape(alias)}[»\"“]?"
    return (
        # «сообщили РИА Новости в силовых структурах», «рассказал ТАСС источник»
        re.compile(rf"(?<!\w){_VERB}\s+{name}(?!\w)", flags),
        # «источник ТАСС», «собеседник „Известий“», «источники РБК в правительстве»
        re.compile(rf"(?<!\w)(?:источник|собеседник)\w*\s+{name}(?!\w)", flags),
        # «в беседе с РИА Новости», «в интервью ТАСС»
        re.compile(rf"(?<!\w)в\s+(?:беседе|интервью|разговоре)\s+(?:с\s+)?{name}(?!\w)", flags),
    )


def own_names(node: Node, sources: dict[int, SourceInfo]) -> set[str]:
    """Как называют само издание: его имя и алиасы, а также всех источников того же владельца
    (сайт и Telegram-канал одной редакции)."""
    if node.source is None:
        return set()
    owner = node.source.owner
    same = [s for s in sources.values() if s.id == node.source.id or (owner and s.owner == owner)]
    return {a for s in same for a in (s.name, *s.aliases) if a and len(a) >= 2}


def cites_own_source(text: str | None, names: set[str]) -> bool:
    if not text or not names:
        return False
    return any(p.search(text) for name in names for p in _patterns(name))
