"""«Проверить через час» на странице отчёта (тарифы со слежением — schedule: true).

Свежий пост сразу после выхода часто даёт «слишком рано»: другие источники ещё не написали
(пост РЕН ТВ 19.10.2026: сразу — одна публикация, позже — семь). Пользователь сам нажимает
кнопку, через час проверяем заново и присылаем результат — в Telegram или письмом. Без нажатия
ничего не отправляем. Повторная проверка — обычная проверка: в лимит и в историю.
В боте то же делает кнопка под ответом (app/bot/telegram.py: check_keyboard).
"""

import json
import logging
import secrets
from datetime import UTC, datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Check, User
from app.models.enums import CheckStatus

log = logging.getLogger("sverka.rechecks")

RECHECK_AFTER = timedelta(hours=1)


class RecheckRefused(Exception):
    pass


def _summary(check: Check) -> dict:
    try:
        return json.loads(check.summary or "{}")
    except ValueError:
        return {}


def pending(check: Check) -> dict | None:
    """Запланированная и ещё не выполненная повторная проверка."""
    recheck = _summary(check).get("recheck")
    return recheck if recheck and not recheck.get("token") else None


async def schedule_recheck(session: AsyncSession, check: Check, user: User, enqueue) -> datetime:
    """Поставить повторную проверку через час. enqueue — app.web.queue.enqueue."""
    from app.orgs.service import membership_of
    from app.plans import can_schedule

    if not can_schedule(user, await membership_of(session, user)):
        raise RecheckRefused("Проверка через час — в тарифах Pro, «Команда» и «Редакция».")
    if check.status != CheckStatus.DONE or "matches" in _summary(check):
        raise RecheckRefused("Повторить можно только завершённую проверку новости.")
    if pending(check):
        raise RecheckRefused("Повторная проверка уже запланирована.")
    at = datetime.now(UTC) + RECHECK_AFTER
    summary = _summary(check)
    summary["recheck"] = {"at": at.isoformat(), "user_id": user.id}
    check.summary = json.dumps(summary, ensure_ascii=False)
    await session.commit()
    await enqueue("hour_recheck", check.id, _defer_by=RECHECK_AFTER, _job_id=f"recheck:{check.id}")
    return at


def _result_line(analysis: dict, publications: int | None) -> str:
    index = analysis.get("index")
    status = analysis.get("status_text") or "готово"
    head = f"{index:g} · {status}" if isinstance(index, int | float) else status
    return f"{head} (публикаций об этом: {publications})" if publications else head


def notice_text(origin: Check, again: Check):
    from html import escape

    from app.watches import Notice, _report_url

    before = (_summary(origin).get("analysis") or {}).get("status_text") or "—"
    report = _summary(again)
    title = (report.get("seed") or {}).get("title") or "новость"
    title = title if len(title) <= 90 else title[:89] + "…"
    now = _result_line(
        report.get("analysis") or {}, report.get("core_publications", report.get("publications"))
    )
    url = _report_url(again)
    html = "\n".join(
        [
            f"⏰ <b>Проверка через час:</b> {escape(title)}",
            "",
            f"Было: {escape(before)}",
            f"Сейчас: {escape(now)}",
            f"Отчёт: {escape(url)}",
        ]
    )
    text = "\n".join(
        [f"Проверка через час: «{title}»", "", f"Было: {before}", f"Сейчас: {now}", f"Отчёт: {url}"]
    )
    return Notice(html, text, f"Сверка: проверка через час — {title}")


async def run_recheck(session: AsyncSession, check_id: int, embedder, llm, bot=None) -> str:
    from app.bot.service import check_refusal
    from app.watches import Notice, notify
    from app.web.checks import run_web_check

    origin = await session.get(Check, check_id)
    plan = pending(origin) if origin else None
    if origin is None or plan is None:
        return "skip"
    user = await session.get(User, plan.get("user_id") or origin.user_id)
    if user is None:
        return "skip"
    if refusal := await check_refusal(session, user):
        await notify(user, Notice(refusal, refusal, "Сверка: проверка через час не выполнена"), bot)
        return "quota"
    again = Check(
        channel=origin.channel if origin.channel in ("web", "bot", "api") else "web",
        user_id=user.id,
        org_id=origin.org_id,
        input_type=origin.input_type,
        input_raw=origin.input_raw,
        status=CheckStatus.QUEUED,
        public_token=secrets.token_urlsafe(12),
    )
    session.add(again)
    await session.commit()
    again = await run_web_check(session, again.id, embedder, llm, use_cache=False)
    if again.status != CheckStatus.DONE:
        return "failed"
    origin = await session.get(Check, check_id)
    report = _summary(again)
    analysis = report.get("analysis") or {}
    summary = _summary(origin)
    summary["recheck"] = {
        **(summary.get("recheck") or {}),
        "token": again.public_token,
        "index": analysis.get("index"),
        "status_text": analysis.get("status_text"),
        "publications": report.get("core_publications", report.get("publications")),
    }
    origin.summary = json.dumps(summary, ensure_ascii=False)
    await session.commit()
    await notify(user, notice_text(origin, again), bot)
    return analysis.get("status") or "done"
