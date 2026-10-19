"""Разбор robots.txt (RFC 9309, как у Google) — для отметки в реестре, не для блокировки.

20.10.2026 выяснилось, что сборщик robots.txt не читал вовсе: Reuters прямо запрещает
автоматический сбор, ura.news закрыт для всех роботов. Решение владельца: robots.txt сбор
не блокирует (многие сайты закрыты для всех, кроме поисковиков), но у каждого источника
видно, что запрещено, — администратор решает сам. Явные письменные запреты (Reuters)
соблюдаем вручную: источник выключен. Техническую защиту не обходим никогда.

Правила разбора — по RFC 9309: группа для нашего робота (SverkaBot), иначе для «*»;
побеждает самое длинное совпадение пути, при равенстве — Allow; поддерживаются «*» и «$».
robots.txt недоступен с кодом 4xx — ограничений нет (так по стандарту); 5xx и сетевые ошибки —
статус неизвестен, пробуем позже.

Ссылку, которую прислал пользователь, открываем по его просьбе, как браузер, — robots.txt
к этому не относится.
"""

import re
import time
from dataclasses import dataclass, field
from urllib.parse import urlsplit

import httpx

BOT = "sverkabot"
CACHE_SECONDS = 24 * 3600
MAX_BYTES = 500_000  # RFC 9309: читать не меньше 500 КиБ


@dataclass
class Rules:
    rules: list[tuple[bool, str]] = field(default_factory=list)  # (allow, шаблон пути)
    disallow_all: bool = False

    def allows(self, url: str) -> bool:
        if self.disallow_all:
            return False
        parts = urlsplit(url)
        path = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
        best: tuple[int, bool] | None = None
        for allow, pattern in self.rules:
            if not pattern:
                continue  # пустой Disallow ничего не запрещает
            if _match(pattern, path):
                key = (len(pattern), allow)
                if best is None or key > best:
                    best = key
        return True if best is None else best[1]


def _match(pattern: str, path: str) -> bool:
    regex = re.escape(pattern).replace(r"\*", ".*")
    if regex.endswith(r"\$"):
        regex = regex[:-2] + "$"
    return re.match(regex, path) is not None


def parse(text: str, bot: str = BOT) -> Rules:
    """Правила для нашего робота: его группа, если она есть, иначе группа «*»."""
    groups: dict[str, list[tuple[bool, str]]] = {}
    agents: list[str] = []
    in_rules = False
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if ":" not in line:
            continue
        key, value = (p.strip() for p in line.split(":", 1))
        key = key.lower()
        if key == "user-agent":
            if in_rules:
                agents, in_rules = [], False
            agents.append(value.lower())
            for agent in agents:
                groups.setdefault(agent, [])
        elif key in ("allow", "disallow") and agents:
            in_rules = True
            for agent in agents:
                groups[agent].append((key == "allow", value))
    mine = [rules for agent, rules in groups.items() if agent == bot]
    if mine:
        return Rules([r for rules in mine for r in rules])
    return Rules(groups.get("*", []))


_cache: dict[str, tuple[float, Rules]] = {}


async def rules_for(client: httpx.AsyncClient, url: str) -> Rules | None:
    parts = urlsplit(url)
    origin = f"{parts.scheme}://{parts.netloc}"
    cached = _cache.get(origin)
    if cached and cached[0] > time.monotonic():
        return cached[1]
    try:
        response = await client.get(origin + "/robots.txt")
        if response.status_code == 200:
            rules = parse(response.content[:MAX_BYTES].decode("utf-8", "replace"))
        elif 400 <= response.status_code < 500:
            rules = Rules()  # нет robots.txt — ограничений нет
        else:
            return None  # сервер не ответил — статус неизвестен
    except httpx.HTTPError:
        return None
    _cache[origin] = (time.monotonic() + CACHE_SECONDS, rules)
    return rules


async def note(client: httpx.AsyncClient, feed_url: str, article_url: str | None) -> str | None:
    """Отметка для реестра: что из нужного нам robots.txt запрещает; None — всё разрешено.

    Пустая строка — статус узнать не удалось (оставляем прежнюю отметку).
    """
    closed = []
    for what, url in (("лента", feed_url), ("статьи", article_url)):
        if not url:
            continue
        rules = await rules_for(client, url)
        if rules is None:
            return ""
        if not rules.allows(url):
            closed.append(what)
    return f"robots.txt запрещает: {', '.join(closed)}" if closed else None
