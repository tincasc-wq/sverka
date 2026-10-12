"""Граф заимствований внутри истории и первоисточник (ТЗ 6.3). Чистые функции, без БД.

Ребро A → B значит «B взял у A»:
- explicit_link — в B ссылка на A (или на источник A, если сама публикация A есть в истории);
- attribution — в B «по данным / сообщает A» (имя источника из его алиасов);
- near_copy — тексты почти совпадают (simhash), A увиден раньше B.
"""

import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from functools import lru_cache

from app.collectors.fingerprint import hamming
from app.collectors.urls import domain_of, normalize_url
from app.core.scoring import GraphConfig
from app.models.enums import DerivationKind


@dataclass(frozen=True)
class SourceInfo:
    id: int
    name: str
    handle_or_domain: str
    type: str
    owner: str | None = None
    aliases: tuple[str, ...] = ()
    tracked_since: datetime | None = None  # с какого момента сборщик следит за источником
    legal_note: str | None = None  # «заблокировано в РФ» и т.п. — показывается в выдаче

    @property
    def display_name(self) -> str:
        return f"{self.name} ({self.legal_note})" if self.legal_note else self.name

    @property
    def group(self) -> str:
        """Группа владения: публикации одной группы — одно подтверждение."""
        return self.owner or self.handle_or_domain


@dataclass(frozen=True)
class Node:
    id: int
    url: str
    observed_at: datetime  # first_seen_at — когда сборщик увидел публикацию
    claimed_at: datetime | None = None  # published_at — что заявляет источник
    source: SourceInfo | None = None
    title: str | None = None
    text: str | None = None
    links: tuple[str, ...] = ()
    simhash: int | None = None

    @property
    def group(self) -> str:
        if self.source:
            return self.source.group
        domain = domain_of(self.url)
        if domain == "t.me":
            # Канал вне реестра: «@Warhronika», а не просто «t.me» — каналы разные.
            return "@" + self.url.split("/")[3]
        return domain

    @property
    def word_count(self) -> int:
        return len((self.text or "").split())

    def effective_at(self, cfg: GraphConfig) -> datetime:
        """Время для хронологии: заявленное, если оно не позже того, что мы видели сами.

        Заявленная дата в будущем относительно нашего наблюдения невозможна — тогда верим себе.
        """
        tolerance = timedelta(minutes=cfg.clock_tolerance_minutes)
        if self.claimed_at and self.claimed_at <= self.observed_at + tolerance:
            return self.claimed_at
        return self.observed_at

    def date_unreliable(self, cfg: GraphConfig) -> bool:
        """Заявленная дата расходится с нашим наблюдением больше допустимого (6.3).

        Публикации старше начала слежения за источником (первый проход сборщика) — не
        подозрительны: мы их просто раньше не видели.
        """
        if not self.claimed_at:
            return False
        limit = timedelta(hours=cfg.unreliable_date_hours)
        if self.claimed_at - self.observed_at > limit:
            return True  # дата из будущего относительно нашего наблюдения
        tracked_since = self.source.tracked_since if self.source else None
        if tracked_since and self.claimed_at < tracked_since:
            return False
        return self.observed_at - self.claimed_at > limit


@dataclass(frozen=True)
class Edge:
    from_id: int
    to_id: int
    kind: DerivationKind
    confidence: float


@dataclass
class GraphResult:
    edges: list[Edge]
    primary_id: int | None
    # Источники, на которые ссылаются публикации истории, но которых в истории нет.
    external_attributions: dict[int, list[str]] = field(default_factory=dict)
    independent_groups: list[str] = field(default_factory=list)

    def incoming(self, node_id: int) -> list[Edge]:
        return [e for e in self.edges if e.to_id == node_id]


# «по данным ТАСС», «сообщает РБК», «как передаёт „Интерфакс“», «пишут „Известия“»,
# «со ссылкой на Mash», «выяснили „Ведомости“», «рассказали в МЧС».
_ATTRIBUTION = re.compile(
    r"по данным|по информации|по сведениям|со ссылкой на|ссылаясь на|"
    r"сообща\w*|сообщил\w*|переда[её]т\w*|передал\w*|пиш\w+|пишет|информиру\w*|"
    r"рассказал\w*|выяснил\w*|узнал\w*|уточнил\w*|заявил\w*|утвержда\w*|"
    # англоязычные: «according to Reuters», «citing Bloomberg», «reported by CNN», «told Axios»
    r"according to|citing|cited by|reported\w*|reports|told|quoted by",
    re.IGNORECASE,
)


# Ссылка на официальный документ без названия источника: «следует из документа»,
# «говорится в указе», «постановление опубликовано на портале правовой информации».
_OFFICIAL_DOCUMENT = re.compile(
    r"(?<!\w)указ(?:а|е|у|ом|ы|ов|ами|ах)?(?!\w)|постановлени\w*|распоряжени\w*|приказ\w*|(?:следует|говорится|сказано)\s+в\s+"
    r"(?:документе|сообщении)|из\s+документа|соответствующ\w+\s+документ\w*|"
    r"портал\w*\s+правов\w*|официальн\w+\s+(?:сайт|портал)",
    re.IGNORECASE,
)


def cites_official_document(text: str) -> bool:
    return bool(text and _OFFICIAL_DOCUMENT.search(text))


# «сообщает Wyborcza», «со ссылкой на Reuters», «по данным МВД», «пишет газета „Известия“».
# Формула атрибуции — без учёта регистра, имя источника — с заглавной буквы.
_NAMED_ATTRIBUTION = re.compile(
    r"(?i:сообща\w*|пиш\w+|по данным|по информации|со ссылкой на|ссылаясь на|"
    r"переда[её]т\w*|передал\w*|according to|citing|reported by|told)\s+"
    r"(?:(?i:газет|издани|агентств|телеграм-канал|канал|портал|сайт)\w*\s+)?[«\"]?"
    # Имя — до четырёх слов с заглавной через пробел (The Wall Street Journal);
    # перевод строки имя обрывает.
    r"([A-ZА-ЯЁ][\w.\-]*(?:[ \t]+[A-ZА-ЯЁ][\w.\-]*){0,3})"
)


def attributed_names(text: str) -> list[str]:
    """Имена источников после формулы атрибуции — в том числе тех, кого нет в реестре."""
    names: list[str] = []
    for match in _NAMED_ATTRIBUTION.finditer(text or ""):
        name = match.group(1).rstrip(".-,")
        if name and name not in names:
            names.append(name)
    return names


@lru_cache(maxsize=4096)
def _alias_pattern(alias: str) -> re.Pattern:
    # Короткие аббревиатуры (РГ, МК, Ъ) — с учётом регистра, иначе ловят куски слов.
    flags = 0 if len(alias) <= 3 else re.IGNORECASE
    return re.compile(rf"(?<!\w){re.escape(alias)}(?!\w)", flags)


def attributed_sources(text: str, sources: list[SourceInfo], window: int) -> set[int]:
    """Id источников, на которые текст ссылается формулой атрибуции."""
    if not text:
        return set()
    spans = [
        (max(0, m.start() - window // 2), m.end() + window) for m in _ATTRIBUTION.finditer(text)
    ]
    if not spans:
        return set()
    found = set()
    for source in sources:
        for alias in source.aliases:
            pattern = _alias_pattern(alias)
            if any(pattern.search(text, lo, hi) for lo, hi in spans):
                found.add(source.id)
                break
    return found


def _link_targets(link: str, nodes: list[Node]) -> tuple[Node | None, set[int]]:
    """Публикация, на которую ведёт ссылка, или источник (по домену / каналу)."""
    url = normalize_url(link)
    exact = next((n for n in nodes if n.url == url), None)
    if exact:
        return exact, set()
    domain = domain_of(url)
    if domain == "t.me":
        channel = "@" + url.split("/")[3] if len(url.split("/")) > 3 else ""
        sources = {n.source.id for n in nodes if n.source and n.source.handle_or_domain == channel}
    else:
        sources = {n.source.id for n in nodes if n.source and n.source.handle_or_domain == domain}
    return None, sources


def build_graph(
    nodes: list[Node], known_sources: list[SourceInfo], cfg: GraphConfig
) -> GraphResult:
    order = sorted(nodes, key=lambda n: (n.effective_at(cfg), n.id))
    rank = {n.id: i for i, n in enumerate(order)}
    by_source: dict[int, list[Node]] = defaultdict(list)
    for n in order:
        if n.source:
            by_source[n.source.id].append(n)

    edges: dict[tuple[int, int], Edge] = {}

    def add(src: Node, dst: Node, kind: DerivationKind, conf: float | None = None) -> None:
        if src.id == dst.id or src.group == dst.group:
            return  # внутри одной группы владения это не заимствование, а одна редакция
        key = (src.id, dst.id)
        conf = conf if conf is not None else cfg.confidence[kind.value]
        if key not in edges or edges[key].confidence < conf:
            edges[key] = Edge(src.id, dst.id, kind, conf)

    def earliest_before(source_ids: set[int], dst: Node) -> Node | None:
        candidates = [n for sid in source_ids for n in by_source.get(sid, [])]
        candidates = [n for n in candidates if rank[n.id] < rank[dst.id]]
        return min(candidates, key=lambda n: rank[n.id], default=None)

    external: dict[int, list[str]] = defaultdict(list)
    official = [n for n in order if n.source and n.source.type == "official"]
    for dst in order:
        for link in dst.links:
            target, source_ids = _link_targets(link, order)
            if target:
                add(target, dst, DerivationKind.EXPLICIT_LINK)
            elif src := earliest_before(source_ids, dst):
                add(src, dst, DerivationKind.EXPLICIT_LINK)

        others = [s for s in known_sources if not dst.source or s.group != dst.source.group]
        for sid in attributed_sources(dst.text or "", others, cfg.attribution_window_chars):
            if src := earliest_before({sid}, dst):
                add(src, dst, DerivationKind.ATTRIBUTION)
            else:
                name = next(s.name for s in others if s.id == sid)
                external[dst.id].append(name)

        # Источники вне реестра («сообщает Wyborcza») — в историю их не связать, но назвать надо.
        # Известные источники уже разобраны по алиасам; «KP.RU» — тоже известный (по домену).
        known = {a.lower() for s in known_sources for a in (*s.aliases, s.name, s.handle_or_domain)}
        for name in attributed_names(dst.text or ""):
            if name.lower() not in known and name not in external[dst.id]:
                external[dst.id].append(name)

        # Пересказ официального документа: источник — самая ранняя официальная публикация истории.
        if dst not in official and cites_official_document(dst.text or ""):
            if src := min(
                (n for n in official if rank[n.id] < rank[dst.id]),
                key=lambda n: rank[n.id],
                default=None,
            ):
                add(src, dst, DerivationKind.ATTRIBUTION, cfg.confidence["official_document"])

    for i, a in enumerate(order):
        for b in order[i + 1 :]:
            if a.simhash is None or b.simhash is None or (a.id, b.id) in edges:
                continue
            # На коротких текстах simhash вырождается: «Пожар.» и «Пожар!» — не перепечатка.
            if min(a.word_count, b.word_count) < cfg.near_copy_min_words:
                continue
            if hamming(a.simhash, b.simhash) <= cfg.near_copy_max_hamming:
                add(a, b, DerivationKind.NEAR_COPY)

    edge_list = sorted(edges.values(), key=lambda e: (rank[e.to_id], rank[e.from_id]))
    has_incoming = {e.to_id for e in edge_list}
    roots = [n for n in order if n.id not in has_incoming]
    independent: list[str] = []
    for n in roots:
        if n.group not in independent:
            independent.append(n.group)

    return GraphResult(
        edges=edge_list,
        primary_id=roots[0].id if roots else None,
        external_attributions=dict(external),
        independent_groups=independent,
    )
