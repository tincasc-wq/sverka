"""Мониторинг тем организации (тариф «Мониторинг»): бренды, персоны, ведомства, сюжеты.

Каждые 10 минут воркер просматривает новые публикации архива и раскладывает их по темам:
- по ключевым словам — с учётом окончаний («Москва» найдёт «Москве», «Газпром» — «Газпрома»);
- по смыслу — если у темы есть описание, публикация близка к нему по вектору;
- слова-исключения отбрасывают находку («Апple» без «яблок»).
Языковая модель не нужна — мониторинг бесплатен по себестоимости и не тратит квоту проверок.

Оповещения (Telegram или почта подписанным сотрудникам) группируют находки в сюжеты
(одно событие у многих изданий) и для каждого называют, кто написал первым:
- instant — сразу, но не чаще раза в 30 минут;
- digest — сводка раз в день в выбранный час по Москве;
- spikes — только всплески.
Всплеск — за последний час публикаций по теме втрое больше обычного часа и не меньше пяти;
о нём пишем в любом режиме, не чаще раза в 6 часов.
"""

import logging
import math
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from html import escape

from sqlalchemy import func, select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.models import Document, Organization, Source, Topic, TopicHit, TopicSubscriber, User
from app.plans import MSK, get_plans

log = logging.getLogger("sverka.topics")

FIRST_SCAN = timedelta(hours=24)  # новая тема сразу показывает сутки (без оповещений)
EMBED_LAG = timedelta(minutes=2)  # вектор публикации считается за минуту — даём запас
MAX_HITS_PER_SCAN = 300  # больше — тема слишком широкая, предупреждаем
INSTANT_EVERY = timedelta(minutes=30)
SPIKE_MIN = 5
SPIKE_FACTOR = 3
SPIKE_EVERY = timedelta(hours=6)
SAME_STORY = 0.85  # косинус: публикации об одном событии
MAX_STORIES_IN_NOTICE = 8
MODES = {
    "instant": "сразу (не чаще раза в 30 минут)",
    "digest": "сводка раз в день",
    "spikes": "только всплески",
}

_STEM_END = re.compile(r"[аяоеёиыуюйьъ]+$")
_CYRILLIC_WORD = re.compile(r"[а-яё]+")


def _word(word: str) -> str:
    w = word.lower()
    if _CYRILLIC_WORD.fullmatch(w) and len(w) >= 5:
        w = _STEM_END.sub("", w) or w  # «москва» → «москв»: найдёт «Москве», «Москвы»
    return re.escape(w) + r"\w*"


def keyword_pattern(phrase: str) -> str | None:
    """Регулярное выражение PostgreSQL для фразы: каждое слово с начала слова, любое окончание."""
    words = re.findall(r"\w+", phrase)
    if not words:
        return None
    return r"\m" + r"\W+".join(_word(w) for w in words)


def py_pattern(pattern: str) -> re.Pattern:
    """То же выражение для Python (тесты и подсветка): \\m в PostgreSQL — начало слова."""
    return re.compile(pattern.replace(r"\m", r"\b"), re.IGNORECASE)


def parse_list(raw: str) -> list[str]:
    """«Газпром, Миллер; Газпром нефть» → список фраз без повторов и пустых."""
    seen, out = set(), []
    for part in re.split(r"[,;\n]", raw or ""):
        phrase = " ".join(part.split())[:100]
        if phrase and phrase.lower() not in seen and keyword_pattern(phrase):
            seen.add(phrase.lower())
            out.append(phrase)
    return out[:30]


def topics_limit(org: Organization, user: User | None = None) -> int:
    from app.web.auth import is_staff

    plan = get_plans().get(org.plan)
    limit = plan.topics if plan else 0
    return max(limit, 20) if user is not None and is_staff(user) else limit


# ---- поиск находок ----

_DOC_TEXT = "coalesce(d.title, '') || ' ' || coalesce(d.text, '')"
_WINDOW = (
    "d.first_seen_at > :since and d.first_seen_at <= :until and d.url not like 'user-text://%'"
)


async def _window_end(session: AsyncSession, since: datetime, now: datetime) -> datetime:
    """До какого момента просматривать: публикации без вектора ждём, иначе пропустим их смысл."""
    until = now - EMBED_LAG
    waiting = await session.scalar(
        select(func.min(Document.first_seen_at)).where(
            Document.first_seen_at > since,
            Document.first_seen_at <= until,
            Document.embedding.is_(None),
            Document.title.is_not(None) | Document.text.is_not(None),
            Document.first_seen_at > now - timedelta(minutes=30),  # застрявшие — не ждём вечно
        )
    )
    if waiting is not None:
        until = waiting - timedelta(microseconds=1)
    return max(until, since)


def _exclude_sql(topic: Topic, params: dict) -> str:
    patterns = [p for p in map(keyword_pattern, topic.exclude or []) if p]
    if not patterns:
        return ""
    params["exclude"] = "(" + ")|(".join(patterns) + ")"
    return f" and not ({_DOC_TEXT}) ~* :exclude"


async def scan_topic(session: AsyncSession, topic: Topic, embedder, now: datetime) -> int:
    """Раскладывает новые публикации в тему. Возвращает число новых находок."""
    first = topic.scanned_until is None
    since = topic.scanned_until or now - FIRST_SCAN
    until = await _window_end(session, since, now)
    if until <= since:
        return 0
    found: dict[int, dict] = {}
    for phrase in topic.keywords or []:
        pattern = keyword_pattern(phrase)
        if not pattern:
            continue
        params = {"since": since, "until": until, "p": pattern}
        sql = f"select d.id from documents d where {_WINDOW} and ({_DOC_TEXT}) ~* :p"
        sql += _exclude_sql(topic, params) + f" limit {MAX_HITS_PER_SCAN + 1}"
        for (doc_id,) in await session.execute(text(sql), params):
            found.setdefault(doc_id, {"matched": "keyword", "keyword": phrase, "score": None})
    if topic.description and topic.description_vec is None and embedder is not None:
        topic.description_vec = embedder.embed([topic.description])[0]
    if topic.description and topic.description_vec is not None:
        params = {
            "since": since,
            "until": until,
            "vec": str(list(topic.description_vec)),
            "dist": 1 - topic.similarity,
        }
        sql = (
            "select d.id, 1 - (d.embedding <=> cast(:vec as vector)) as sim from documents d "
            f"where {_WINDOW} and d.embedding is not null "
            "and d.embedding <=> cast(:vec as vector) <= :dist"
        )
        sql += _exclude_sql(topic, params) + f" limit {MAX_HITS_PER_SCAN + 1}"
        for doc_id, sim in await session.execute(text(sql), params):
            hit = found.setdefault(doc_id, {"matched": "meaning", "keyword": None, "score": None})
            hit["score"] = round(float(sim), 3)
    if len(found) > MAX_HITS_PER_SCAN:
        topic.note = (
            f"Тема слишком широкая: за {int((until - since).total_seconds() // 60)} мин "
            f"больше {MAX_HITS_PER_SCAN} публикаций. Уточните слова или добавьте исключения."
        )
        found = dict(list(found.items())[:MAX_HITS_PER_SCAN])
    elif found or first:
        topic.note = None
    added = 0
    if found:
        rows = [
            {"topic_id": topic.id, "document_id": doc_id, "notified": first, **hit}
            for doc_id, hit in found.items()
        ]
        result = await session.execute(
            insert(TopicHit).values(rows).on_conflict_do_nothing().returning(TopicHit.id)
        )
        added = len(result.all())
    topic.scanned_until = until
    return added


# ---- сюжеты: одно событие у многих изданий ----


@dataclass
class Item:
    doc: Document
    source: str
    when: datetime


@dataclass
class StoryGroup:
    items: list[Item] = field(default_factory=list)

    @property
    def first(self) -> Item:
        return min(self.items, key=lambda i: i.when)

    @property
    def sources(self) -> list[str]:
        return sorted({i.source for i in self.items})


def _cos(a, b) -> float:
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    norm = math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b))
    return dot / norm if norm else 0.0


def group_stories(items: list[Item]) -> list[StoryGroup]:
    """Жадная группировка по близости векторов; сюжеты — по числу изданий, крупные сверху."""
    groups: list[tuple[list[float] | None, StoryGroup]] = []
    for item in sorted(items, key=lambda i: i.when):
        vec = item.doc.embedding
        target = None
        if vec is not None:
            for center, group in groups:
                if center is not None and _cos(center, vec) >= SAME_STORY:
                    target = group
                    break
        if target is None:
            target = StoryGroup()
            groups.append((list(vec) if vec is not None else None, target))
        target.items.append(item)
    return sorted((g for _, g in groups), key=lambda g: (-len(g.sources), g.first.when))


async def load_items(session: AsyncSession, hit_ids: list[int]) -> list[Item]:
    rows = await session.execute(
        select(Document, Source.name)
        .join(TopicHit, TopicHit.document_id == Document.id)
        .outerjoin(Source, Source.id == Document.source_id)
        .where(TopicHit.id.in_(hit_ids))
    )
    return [
        Item(doc, name or doc.url.split("/")[2], doc.published_at or doc.first_seen_at)
        for doc, name in rows
    ]


# ---- оповещения ----


def topic_url(topic: Topic) -> str:
    return f"{get_settings().public_base_url.rstrip('/')}/org/topics/{topic.id}"


def _title(doc: Document) -> str:
    return " ".join((doc.title or doc.text or doc.url).split())[:140]


def notice_for(topic: Topic, groups: list[StoryGroup], head: str):
    from app.watches import Notice

    total = sum(len(g.items) for g in groups)
    lines_html = [f"🔎 <b>{escape(head)}</b>", f"Публикаций: {total}, сюжетов: {len(groups)}.", ""]
    lines_text = [head, f"Публикаций: {total}, сюжетов: {len(groups)}.", ""]
    for n, group in enumerate(groups[:MAX_STORIES_IN_NOTICE], 1):
        first = group.first
        when = first.when.astimezone(MSK).strftime("%d.%m %H:%M")
        others = len(group.sources) - 1
        tail = f", ещё изданий: {others}" if others > 0 else ""
        lines_html.append(
            f'{n}. <a href="{escape(first.doc.url)}">{escape(_title(first.doc))}</a>\n'
            f"   первым: {escape(first.source)}, {when}{tail}"
        )
        lines_text.append(
            f"{n}. {_title(first.doc)}\n   первым: {first.source}, {when}{tail}\n   {first.doc.url}"
        )
    if len(groups) > MAX_STORIES_IN_NOTICE:
        rest = len(groups) - MAX_STORIES_IN_NOTICE
        lines_html.append(f"…и ещё сюжетов: {rest}")
        lines_text.append(f"…и ещё сюжетов: {rest}")
    url = topic_url(topic)
    lines_html += ["", f"Все находки: {escape(url)}"]
    lines_text += ["", f"Все находки: {url}"]
    return Notice(html="\n".join(lines_html), text="\n".join(lines_text), subject=f"Сверка: {head}")


async def recipients(session: AsyncSession, topic: Topic) -> list[User]:
    return list(
        await session.scalars(
            select(User)
            .join(TopicSubscriber, TopicSubscriber.user_id == User.id)
            .where(TopicSubscriber.topic_id == topic.id)
        )
    )


async def _send(session: AsyncSession, topic: Topic, notice, bot) -> int:
    from app.watches import notify

    sent = 0
    for user in await recipients(session, topic):
        sent += bool(await notify(user, notice, bot))
    return sent


async def _spike(session: AsyncSession, topic: Topic, now: datetime) -> list[int] | None:
    """Находки последнего часа, если это всплеск; иначе None."""
    if topic.last_spike_at and now - topic.last_spike_at < SPIKE_EVERY:
        return None
    if now - topic.created_at < timedelta(hours=2):
        return None  # у новой темы ещё нет понятия «обычного часа»
    hour_ago, week_ago = now - timedelta(hours=1), now - timedelta(days=7)
    rows = (
        await session.execute(
            select(TopicHit.id, Document.first_seen_at)
            .join(Document, Document.id == TopicHit.document_id)
            .where(TopicHit.topic_id == topic.id, Document.first_seen_at > week_ago)
        )
    ).all()
    recent = [hit_id for hit_id, seen in rows if seen > hour_ago]
    older = len(rows) - len(recent)
    hours = max(
        1.0, min(7 * 24.0, (hour_ago - max(week_ago, topic.created_at)).total_seconds() / 3600)
    )
    usual = older / hours
    if len(recent) >= max(SPIKE_MIN, SPIKE_FACTOR * usual):
        return recent
    return None


def _digest_due(topic: Topic, now: datetime) -> bool:
    local = now.astimezone(MSK)
    if local.hour != topic.digest_hour:
        return False
    return topic.last_sent_at is None or topic.last_sent_at.astimezone(MSK).date() < local.date()


async def deliver(session: AsyncSession, topic: Topic, now: datetime, bot=None) -> int:
    """Отправляет, что пора: всплеск — в любом режиме, остальное — по режиму темы."""
    sent = 0
    spike = await _spike(session, topic, now)
    if spike:
        groups = group_stories(await load_items(session, spike))
        notice = notice_for(topic, groups, f"Всплеск по теме «{topic.name}» за последний час")
        sent += await _send(session, topic, notice, bot)
        topic.last_spike_at = now
    pending = list(
        await session.scalars(
            select(TopicHit.id).where(TopicHit.topic_id == topic.id, TopicHit.notified.is_(False))
        )
    )
    if not pending:
        return sent
    due = {
        "instant": topic.last_sent_at is None or now - topic.last_sent_at >= INSTANT_EVERY,
        "digest": _digest_due(topic, now),
        "spikes": True,  # в этом режиме обычные находки не рассылаем — просто отмечаем
    }[topic.mode]
    if not due:
        return sent
    if topic.mode != "spikes":
        groups = group_stories(await load_items(session, pending))
        head = (
            f"Сводка по теме «{topic.name}» за сутки"
            if topic.mode == "digest"
            else f"Новое по теме «{topic.name}»"
        )
        sent += await _send(session, topic, notice_for(topic, groups, head), bot)
        topic.last_sent_at = now
    await session.execute(
        TopicHit.__table__.update().where(TopicHit.id.in_(pending)).values(notified=True)
    )
    return sent


async def run_topics(session_factory, embedder, bot=None, now: datetime | None = None) -> dict:
    now = now or datetime.now(UTC)
    stats = {"topics": 0, "hits": 0, "sent": 0}
    async with session_factory() as session:
        ids = list(
            await session.scalars(
                select(Topic.id)
                .join(Organization, Organization.id == Topic.org_id)
                .where(Topic.active.is_(True), Organization.active.is_(True))
            )
        )
    for topic_id in ids:
        async with session_factory() as session:
            topic = await session.get(Topic, topic_id)
            org = await session.get(Organization, topic.org_id)
            if topics_limit(org) <= 0:
                continue  # тариф без мониторинга: темы хранятся, но не работают
            try:
                stats["hits"] += await scan_topic(session, topic, embedder, now)
                await session.commit()
                stats["sent"] += await deliver(session, topic, now, bot)
                await session.commit()
                stats["topics"] += 1
            except Exception:  # noqa: BLE001 — одна тема не должна останавливать остальные
                await session.rollback()
                log.exception("тема %s: ошибка", topic_id)
    return stats


# ---- кабинет ----


async def org_topics(session: AsyncSession, org_id: int) -> list[Topic]:
    return list(
        await session.scalars(select(Topic).where(Topic.org_id == org_id).order_by(Topic.id))
    )


async def hit_counts(session: AsyncSession, topic_ids: list[int], hours: int = 24) -> dict:
    if not topic_ids:
        return {}
    rows = await session.execute(
        select(TopicHit.topic_id, func.count())
        .join(Document, Document.id == TopicHit.document_id)
        .where(
            TopicHit.topic_id.in_(topic_ids),
            Document.first_seen_at > datetime.now(UTC) - timedelta(hours=hours),
        )
        .group_by(TopicHit.topic_id)
    )
    return dict(rows.all())


async def recent_groups(session: AsyncSession, topic: Topic, hours: int = 48) -> list[StoryGroup]:
    ids = list(
        await session.scalars(
            select(TopicHit.id)
            .join(Document, Document.id == TopicHit.document_id)
            .where(
                TopicHit.topic_id == topic.id,
                Document.first_seen_at > datetime.now(UTC) - timedelta(hours=hours),
            )
            .order_by(Document.first_seen_at.desc())
            .limit(500)
        )
    )
    return group_stories(await load_items(session, ids)) if ids else []


async def is_subscribed(session: AsyncSession, topic: Topic, user: User) -> bool:
    return bool(
        await session.scalar(
            select(TopicSubscriber.id).where(
                TopicSubscriber.topic_id == topic.id, TopicSubscriber.user_id == user.id
            )
        )
    )


async def set_subscription(session: AsyncSession, topic: Topic, user: User, on: bool) -> None:
    if on:
        await session.execute(
            insert(TopicSubscriber)
            .values(topic_id=topic.id, user_id=user.id)
            .on_conflict_do_nothing()
        )
    else:
        await session.execute(
            TopicSubscriber.__table__.delete().where(
                TopicSubscriber.topic_id == topic.id, TopicSubscriber.user_id == user.id
            )
        )
