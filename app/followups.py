"""Отслеживание поправок: после проверки новость ещё 3 дня перепроверяется сама.

Ошибочная новость часто расходится быстро, а поправка выходит через день-два (случай
Minerva Zen, 14.10.2026: «танкер везёт дизель» — через два дня «везёт удобрения»).
Человек, проверивший новость в первый день, иначе так и остался бы с «подтверждено».

Как работает (крон воркера раз в 30 минут):
- берём проверки людей (бот, сайт) за последние AUTO_HOURS часов;
- не чаще раза в AUTO_EVERY смотрим, появились ли в архиве новые публикации, близкие к
  проверенной новости; если нет — языковую модель не трогаем, это бесплатно;
- если появились — перепроверяем без кэша отдельной проверкой (channel="auto"), в лимит
  пользователя она не идёт;
- если в новой проверке есть поправка, которой не было, или новость стала «оспорено» /
  «есть опровержения», — пишем человеку (Telegram или почта) и ставим в старый отчёт
  ссылку на новый.
"""

import json
import logging
import secrets
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.scoring import get_scoring
from app.models import Check, Document, User
from app.models.enums import CheckStatus

log = logging.getLogger("sverka.followups")

AUTO_HOURS = 72  # сколько следим после проверки
AUTO_EVERY = timedelta(hours=6)  # не чаще
AUTO_MAX_RUNS = 6  # не больше перепроверок на одну проверку
WORSE = {"disputed", "refuted"}


def _analysis(check: Check) -> dict:
    try:
        return json.loads(check.summary or "{}").get("analysis") or {}
    except ValueError:
        return {}


def news_of_correction(old: dict, new: dict) -> list[dict]:
    """Поправки, которых в старом отчёте не было; пусто — писать не о чем."""
    known = {c["url"] for c in old.get("corrections") or []}
    fresh = [c for c in new.get("corrections") or [] if c["url"] not in known]
    if fresh:
        return fresh
    if new.get("status") in WORSE and old.get("status") not in WORSE:
        # Опровержение без явной поправки — сообщаем само изменение статуса.
        return [{"source": None, "status_text": new.get("status_text")}]
    return []


async def due_checks(session: AsyncSession, now: datetime) -> list[Check]:
    last_look = func.coalesce(Check.auto_checked_at, Check.finished_at)
    return list(
        await session.scalars(
            select(Check)
            .where(
                Check.status == CheckStatus.DONE,
                Check.channel.in_(("bot", "web")),
                Check.user_id.is_not(None),
                Check.document_id.is_not(None),
                Check.finished_at >= now - timedelta(hours=AUTO_HOURS),
                Check.auto_runs < AUTO_MAX_RUNS,
                last_look <= now - AUTO_EVERY,
            )
            .order_by(Check.finished_at)
        )
    )


async def new_related(session: AsyncSession, seed: Document, since: datetime) -> int:
    """Сколько новых публикаций близки к новости настолько, что могут войти в её историю."""
    if seed.embedding is None:
        return 0
    max_distance = 1 - get_scoring().clustering.llm_review_from
    return await session.scalar(
        select(func.count())
        .select_from(Document)
        .where(
            Document.id != seed.id,
            Document.first_seen_at > since,
            Document.embedding.is_not(None),
            Document.embedding.cosine_distance(seed.embedding) <= max_distance,
        )
    )


def notice_text(origin: Check, fresh: list[dict], new: Check):
    from html import escape

    from app.watches import Notice, _report_url

    report = json.loads(origin.summary or "{}")
    title = (report.get("seed") or {}).get("title") or "новость"
    title = title if len(title) <= 90 else title[:89] + "…"
    url = _report_url(new)
    lines_html, lines_text = [], []
    for c in fresh:
        if c.get("source"):
            lines_html.append(f"• {escape(c['source'])}: «{escape(c.get('quote') or '')}»")
            lines_text.append(f"— {c['source']}: «{c.get('quote') or ''}»")
        else:
            lines_html.append(f"• теперь: {escape(c.get('status_text') or '')}")
            lines_text.append(f"— теперь: {c.get('status_text') or ''}")
    html = "\n".join(
        [f"⚠️ <b>Поправка к новости, которую вы проверяли:</b> {escape(title)}", ""]
        + lines_html
        + ["", f"Обновлённый отчёт: {escape(url)}"]
    )
    text = "\n".join(
        [f"Поправка к новости, которую вы проверяли: «{title}»", ""]
        + lines_text
        + ["", f"Обновлённый отчёт: {url}"]
    )
    return Notice(html, text, f"Сверка: поправка — {title}")


async def follow_up(session: AsyncSession, origin: Check, embedder, llm, bot=None) -> str:
    from app.watches import notify
    from app.web.checks import run_web_check

    now = datetime.now(UTC)
    since = origin.auto_checked_at or origin.finished_at
    seed = await session.get(Document, origin.document_id)
    origin.auto_checked_at = now
    if seed is None or "matches" in json.loads(origin.summary or "{}"):
        await session.commit()
        return "skip"
    if not await new_related(session, seed, since):
        await session.commit()
        return "quiet"
    origin.auto_runs += 1
    again = Check(
        channel="auto",
        user_id=origin.user_id,
        org_id=origin.org_id,
        input_type=origin.input_type,
        input_raw=origin.input_raw,
        status=CheckStatus.QUEUED,
        public_token=secrets.token_urlsafe(12),
    )
    session.add(again)
    await session.commit()
    origin_id = origin.id
    again = await run_web_check(session, again.id, embedder, llm, use_cache=False)
    if again.status != CheckStatus.DONE:
        return "failed"
    origin = await session.get(Check, origin_id)
    fresh = news_of_correction(_analysis(origin), _analysis(again))
    if not fresh:
        return "same"
    # Старый отчёт ведёт на новый: по ссылке из бота человек увидит поправку.
    summary = json.loads(origin.summary or "{}")
    summary["followup"] = {
        "token": again.public_token,
        "status_text": _analysis(again).get("status_text"),
        "at": now.isoformat(),
    }
    origin.summary = json.dumps(summary, ensure_ascii=False)
    await session.commit()
    user = await session.get(User, origin.user_id)
    if user is not None:
        await notify(user, notice_text(origin, fresh, again), bot)
    return "corrected"


async def run_followups(session_factory, embedder, llm, bot=None) -> dict:
    now = datetime.now(UTC)
    async with session_factory() as session:
        ids = [c.id for c in await due_checks(session, now)]
    result: dict[str, int] = {}
    for check_id in ids:
        async with session_factory() as session:
            origin = await session.get(Check, check_id)
            try:
                outcome = await follow_up(session, origin, embedder, llm, bot)
            except Exception:  # noqa: BLE001 — одна проверка не должна ронять остальные
                log.exception("follow-up of check %s failed", check_id)
                await session.rollback()
                origin = await session.get(Check, check_id)
                origin.auto_checked_at = now
                await session.commit()
                outcome = "error"
        result[outcome] = result.get(outcome, 0) + 1
    return result
