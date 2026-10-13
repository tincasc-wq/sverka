"""Повторная проверка по расписанию (тарифы с schedule: true).

Пользователь ставит новость «на слежение»: каждые N часов воркер проверяет её заново,
без кэша. Пишем только когда картина изменилась — новые публикации, независимые
подтверждения, индекс, первоисточник — и один раз в конце, с итогом за весь срок.
Каждая повторная проверка идёт в лимит тарифа, как обычная.
"""

import json
import logging
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from html import escape

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Check, User, Watch
from app.models.enums import CheckStatus

log = logging.getLogger("sverka.watches")

EVERY_HOURS = (6, 12, 24)
DAYS = (1, 3, 7)
MAX_ACTIVE = 10  # на пользователя: слежение — не способ обойти лимит
INDEX_STEP = 5  # меньшее движение индекса — шум, не повод писать


class WatchRefused(Exception):
    pass


def state_of(check: Check) -> dict:
    """Что сравниваем между прогонами — из сохранённого отчёта."""
    report = json.loads(check.summary or "{}")
    analysis = report.get("analysis") or {}
    primary = report.get("primary") or {}
    return {
        "publications": report.get("core_publications", report.get("publications", 0)),
        "independent": report.get("independent_core", report.get("independent_reports", 0)),
        "index": analysis.get("index"),
        "status_text": analysis.get("status_text"),
        "primary": primary.get("source"),
        "title": (report.get("seed") or {}).get("title"),
    }


def changes(old: dict, new: dict) -> list[str]:
    """Строки «было → стало» по тому, что изменилось заметно."""
    lines = []
    if new["publications"] != old["publications"]:
        lines.append(f"публикаций о событии: {old['publications']} → {new['publications']}")
    if new["independent"] != old["independent"]:
        lines.append(f"независимых групп источников: {old['independent']} → {new['independent']}")
    a, b = old.get("index"), new.get("index")
    if (a is None) != (b is None) or (a is not None and abs(b - a) >= INDEX_STEP):
        fmt = lambda x: "—" if x is None else f"{x:g}"  # noqa: E731
        lines.append(f"индекс подтверждённости: {fmt(a)} → {fmt(b)} ({new.get('status_text')})")
    if new.get("primary") and old.get("primary") and new["primary"] != old["primary"]:
        lines.append(f"первоисточник: {old['primary']} → {new['primary']}")
    return lines


async def active_watch(session: AsyncSession, user: User, check: Check) -> Watch | None:
    return await session.scalar(
        select(Watch).where(
            Watch.user_id == user.id,
            Watch.active.is_(True),
            (Watch.check_id == check.id) | (Watch.last_check_id == check.id),
        )
    )


async def user_watches(session: AsyncSession, user: User) -> list[Watch]:
    return list(
        await session.scalars(
            select(Watch)
            .where(Watch.user_id == user.id, Watch.active.is_(True))
            .order_by(Watch.until)
        )
    )


async def start_watch(
    session: AsyncSession, user: User, check: Check, every_hours: int, days: int
) -> Watch:
    from app.orgs.service import can_view_check, membership_of
    from app.plans import can_schedule

    if not can_schedule(user, await membership_of(session, user)):
        raise WatchRefused(
            "Повторная проверка по расписанию — в тарифах Pro, «Команда», «Редакция»."
        )
    if every_hours not in EVERY_HOURS or days not in DAYS:
        raise WatchRefused("Такого расписания нет.")
    if check.status != CheckStatus.DONE or "matches" in json.loads(
        check.summary or '{"matches": 1}'
    ):
        raise WatchRefused("Следить можно только за завершённой проверкой новости.")
    if not await can_view_check(session, check, user):
        raise WatchRefused("Отчёт не найден.")
    now = datetime.now(UTC)
    watch = await active_watch(session, user, check)
    if watch is None:
        if len(await user_watches(session, user)) >= MAX_ACTIVE:
            raise WatchRefused(
                f"Одновременно можно следить не более чем за {MAX_ACTIVE} новостями."
            )
        state = state_of(check)
        watch = Watch(
            user_id=user.id,
            check_id=check.id,
            last_check_id=check.id,
            first_state=state,
            last_state=state,
            runs=0,
            active=True,
        )
        session.add(watch)
    watch.every_hours = every_hours
    watch.next_run_at = now + timedelta(hours=every_hours)
    watch.until = now + timedelta(days=days)
    await session.commit()
    return watch


async def stop_watch(session: AsyncSession, user: User, watch_id: int) -> bool:
    watch = await session.get(Watch, watch_id)
    if watch is None or watch.user_id != user.id or not watch.active:
        return False
    watch.active = False
    watch.stopped_reason = "остановлено пользователем"
    await session.commit()
    return True


@dataclass
class Notice:
    html: str  # для Telegram
    text: str  # для письма
    subject: str


def _report_url(check: Check) -> str:
    from app.core.config import get_settings

    return f"{get_settings().public_base_url.rstrip('/')}/r/{check.public_token}"


def _title(state: dict) -> str:
    title = state.get("title") or "новость"
    return title if len(title) <= 90 else title[:89] + "…"


def update_notice(state: dict, lines: list[str], check: Check) -> Notice:
    title, url = _title(state), _report_url(check)
    html = "\n".join(
        [f"🔔 <b>Обновление: {escape(title)}</b>", ""]
        + [f"• {escape(x)}" for x in lines]
        + ["", f"Отчёт: {escape(url)}"]
    )
    text = "\n".join(
        [f"Обновление по новости «{title}»:", ""] + [f"— {x}" for x in lines] + ["", url]
    )
    return Notice(html, text, f"Сверка: обновление — {title}")


def final_notice(watch: Watch, check: Check | None) -> Notice:
    title = _title(watch.last_state)
    lines = changes(watch.first_state, watch.last_state) if watch.runs else []
    summary = lines or ["за это время картина не изменилась"]
    reason = "" if watch.stopped_reason in (None, "срок истёк") else f" ({watch.stopped_reason})"
    head = f"Слежение закончено{reason}. Повторных проверок: {watch.runs}."
    url = _report_url(check) if check else ""
    html = "\n".join(
        [f"⏹ <b>{escape(title)}</b>", escape(head), ""]
        + [f"• {escape(x)}" for x in summary]
        + ([f"\nПоследний отчёт: {escape(url)}"] if url else [])
    )
    text = "\n".join(
        [f"«{title}»", head, ""] + [f"— {x}" for x in summary] + ([f"\n{url}"] if url else [])
    )
    return Notice(html, text, f"Сверка: слежение закончено — {title}")


async def notify(user: User, notice: Notice, bot=None) -> bool:
    """В Telegram, если пользователь им пользуется; иначе — письмом."""
    if user.tg_id and bot is not None:
        try:
            await bot.send_message(user.tg_id, notice.html)
            return True
        except Exception:  # noqa: BLE001 — бот заблокирован пользователем и т. п.: попробуем почту
            log.warning("watch notice to tg %s failed", user.tg_id, exc_info=True)
    if user.email:
        from app.web.mail import send_mail

        return await send_mail(user.email, notice.subject, notice.text)
    return False


async def _finish(session: AsyncSession, watch: Watch, user: User, reason: str, bot) -> None:
    watch.active = False
    watch.stopped_reason = reason
    await session.commit()
    last = await session.get(Check, watch.last_check_id) if watch.last_check_id else None
    await notify(user, final_notice(watch, last), bot)


async def run_watch(session: AsyncSession, watch: Watch, embedder, llm, bot=None) -> str:
    """Один прогон слежения. Возвращает, что произошло, — для журнала воркера."""
    from app.bot.service import check_refusal
    from app.orgs.service import membership_of
    from app.plans import can_schedule
    from app.web.checks import run_web_check

    user = await session.get(User, watch.user_id)
    now = datetime.now(UTC)
    if now >= watch.until:
        await _finish(session, watch, user, "срок истёк", bot)
        return "finished"
    membership = await membership_of(session, user)
    if not can_schedule(user, membership):
        await _finish(session, watch, user, "тариф больше не включает слежение", bot)
        return "plan"
    if await check_refusal(session, user):
        await _finish(session, watch, user, "исчерпан лимит проверок", bot)
        return "quota"

    origin = await session.get(Check, watch.check_id)
    check = Check(
        channel="watch",
        user_id=user.id,
        org_id=membership.org.id if membership else None,
        input_type=origin.input_type,
        input_raw=origin.input_raw,
        status=CheckStatus.QUEUED,
        public_token=secrets.token_urlsafe(12),
    )
    session.add(check)
    await session.commit()
    check = await run_web_check(session, check.id, embedder, llm, use_cache=False)

    watch = await session.get(Watch, watch.id)
    watch.runs += 1
    watch.next_run_at = now + timedelta(hours=watch.every_hours)
    if check.status != CheckStatus.DONE:
        await session.commit()
        return "failed"
    new = state_of(check)
    lines = changes(watch.last_state, new)
    watch.last_state = new
    watch.last_check_id = check.id
    await session.commit()
    if lines:
        await notify(user, update_notice(new, lines, check), bot)
        return "changed"
    return "same"


async def run_due_watches(session_factory, embedder, llm, bot=None) -> dict:
    """Крон воркера: все слежения, у которых подошло время (или истёк срок)."""
    now = datetime.now(UTC)
    async with session_factory() as session:
        due = list(
            await session.scalars(
                select(Watch.id)
                .where(Watch.active.is_(True), (Watch.next_run_at <= now) | (Watch.until <= now))
                .order_by(Watch.next_run_at)
            )
        )
    result: dict[str, int] = {}
    for watch_id in due:
        async with session_factory() as session:
            watch = await session.get(Watch, watch_id)
            try:
                outcome = await run_watch(session, watch, embedder, llm, bot)
            except Exception:  # noqa: BLE001 — одно слежение не должно ронять остальные
                log.exception("watch %s failed", watch_id)
                await session.rollback()
                watch = await session.get(Watch, watch_id)
                watch.next_run_at = now + timedelta(hours=watch.every_hours)
                await session.commit()
                outcome = "error"
        result[outcome] = result.get(outcome, 0) + 1
    return result
