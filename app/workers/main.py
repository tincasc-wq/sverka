"""Фоновый воркер arq: сборщики по расписанию и расчёт эмбеддингов (этап 2)."""

import asyncio
import functools
import logging
from collections.abc import Awaitable, Callable

from arq import cron
from arq.connections import RedisSettings

from app.collectors.runner import poll_rss, poll_telegram, summarize
from app.core.config import get_settings
from app.core.db import SessionLocal
from app.embeddings import SentenceTransformerEmbedder, embed_pending
from app.models.enums import InputType
from app.registry.service import recompute_ratings

log = logging.getLogger("sverka.worker")


def single_flight(func: Callable[[dict], Awaitable]) -> Callable[[dict], Awaitable]:
    """Пропускает запуск, если предыдущий ещё идёт: крон не должен копить параллельные копии."""
    lock = asyncio.Lock()

    @functools.wraps(func)
    async def wrapper(ctx: dict):
        if lock.locked():
            return "skipped: previous run in progress"
        async with lock:
            return await func(ctx)

    return wrapper


async def bot_check(
    ctx: dict,
    chat_id: int,
    reply_to: int,
    value: str,
    input_type: str,
    tg_id: int,
    use_cache: bool = True,
    fallback_text: str | None = None,
    fallback_date: str | None = None,
) -> str:
    """Проверка по запросу из бота; ответ уходит прямо в чат (ФТ-7.1, кнопки — ФТ-7.4)."""
    from aiogram.types import ReplyParameters

    from app.bot.service import check_for_chat
    from app.bot.telegram import check_keyboard

    async with SessionLocal() as session:
        reply = await check_for_chat(
            session,
            value,
            InputType(input_type),
            tg_id,
            ctx["embedder"],
            use_cache=use_cache,
            fallback_text=fallback_text,
            fallback_date=fallback_date,
            llm=ctx.get("llm"),
        )
        pdf = watch = False
        if reply.check_id:
            from app.bot.service import get_or_create_user
            from app.orgs.service import membership_of
            from app.plans import can_export_pdf, can_schedule

            user = await get_or_create_user(session, tg_id)
            membership = await membership_of(session, user)
            pdf = can_export_pdf(user, membership)
            watch = can_schedule(user, membership)
    bot = ctx.get("bot")
    if bot is None:
        log.warning("bot token is not set, reply for chat %s dropped", chat_id)
        return "no bot"
    await bot.send_message(
        chat_id,
        reply.text,
        reply_parameters=ReplyParameters(message_id=reply_to, allow_sending_without_reply=True),
        reply_markup=check_keyboard(reply.check_id, pdf, watch) if reply.check_id else None,
    )
    return f"check={reply.check_id}"


async def web_check(ctx: dict, check_id: int, use_cache: bool = True) -> str:
    """Проверка, запущенная с сайта: результат сайт показывает сам, опрашивая статус."""
    from app.web.checks import run_web_check

    async with SessionLocal() as session:
        check = await run_web_check(
            session, check_id, ctx["embedder"], ctx.get("llm"), use_cache=use_cache
        )
    return f"check={check.id} {check.status}"


async def clear_stale_in_progress(redis) -> int:
    """Воркер один: после перезапуска ни одна прежняя задача на самом деле не выполняется.

    Отметки «в работе» от убитого процесса arq держит до истечения таймаута задачи — всё это
    время прерванная проверка висит в очереди. Снимаем их, чтобы задачи пошли заново сразу.
    """
    keys = [key async for key in redis.scan_iter(match="arq:in-progress:*")]
    if keys:
        await redis.delete(*keys)
    return len(keys)


async def startup(ctx: dict) -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(asctime)s %(name)s: %(message)s", "%H:%M:%S"))
    sverka_log = logging.getLogger("sverka")
    sverka_log.addHandler(handler)
    sverka_log.setLevel(logging.INFO)
    if stale := await clear_stale_in_progress(ctx["redis"]):
        log.info("сняты отметки прерванных задач: %d", stale)
    ctx["embedder"] = SentenceTransformerEmbedder()
    from app.llm import LlmClient

    llm = LlmClient()
    ctx["llm"] = llm if llm.available() else None
    log.info("LLM: %s", "подключена" if ctx["llm"] else "нет ключей — проверки без LLM")
    if get_settings().telegram_bot_token:
        from app.bot.telegram import make_bot

        ctx["bot"] = make_bot()


async def shutdown(ctx: dict) -> None:
    if bot := ctx.get("bot"):
        await bot.session.close()


async def ping(ctx: dict) -> str:
    return "pong"


@single_flight
async def collect_rss(ctx: dict) -> str:
    result = summarize(await poll_rss(SessionLocal))
    log.info("rss: %s", result)
    return result


@single_flight
async def collect_telegram(ctx: dict) -> str:
    result = summarize(await poll_telegram(SessionLocal))
    log.info("telegram: %s", result)
    return result


@single_flight
async def embed_documents(ctx: dict) -> int:
    async with SessionLocal() as session:
        count = await embed_pending(session, ctx["embedder"])
    if count:
        log.info("embedded %d documents", count)
    return count


@single_flight
async def run_watches(ctx: dict) -> str:
    """Повторные проверки по расписанию (app/watches.py)."""
    from app.watches import run_due_watches

    result = await run_due_watches(SessionLocal, ctx["embedder"], ctx.get("llm"), ctx.get("bot"))
    if result:
        log.info("watches: %s", result)
    return str(result)


@single_flight
async def follow_up_checks(ctx: dict) -> str:
    """Отслеживание поправок к недавним проверкам (app/followups.py)."""
    from app.followups import run_followups

    result = await run_followups(SessionLocal, ctx["embedder"], ctx.get("llm"), ctx.get("bot"))
    if result:
        log.info("followups: %s", result)
    return str(result)


@single_flight
async def purge_personal_data(ctx: dict) -> str:
    """Сроки хранения из политики обработки ПДн (/privacy): что обещано — то и удаляется."""
    from sqlalchemy import text

    async with SessionLocal() as session:
        tokens = await session.execute(
            text("DELETE FROM login_tokens WHERE created_at < now() - interval '30 days'")
        )
        ips = await session.execute(
            text(
                "UPDATE plan_requests SET ip = NULL "
                "WHERE ip IS NOT NULL AND created_at < now() - interval '90 days'"
            )
        )
        await session.commit()
    result = f"login_tokens={tokens.rowcount}, plan_request_ips={ips.rowcount}"
    log.info("purge: %s", result)
    return result


async def recompute_source_ratings(ctx: dict) -> str:
    async with SessionLocal() as session:
        stats = await recompute_ratings(session)
        await session.commit()
    result = f"sources={stats.sources}, changed={stats.changed}"
    log.info("ratings: %s", result)
    return result


class WorkerSettings:
    functions = [
        ping,
        collect_rss,
        collect_telegram,
        embed_documents,
        recompute_source_ratings,
        bot_check,
        web_check,
        purge_personal_data,
        run_watches,
        follow_up_checks,
    ]
    cron_jobs = [
        # Таймаут с запасом: полные тексты после простоя качаются долго; single_flight не даёт
        # запускам накладываться — пока идёт длинный цикл, пятиминутные просто пропускаются.
        cron(collect_rss, minute=set(range(0, 60, 5)), unique=True, timeout=1500),
        cron(collect_telegram, minute=set(range(2, 60, 10)), unique=True, timeout=600),
        cron(embed_documents, second=30, unique=True, timeout=900),
        # Ночной пересчёт рейтинга (6.6); 03:00 по Москве = 00:00 UTC.
        cron(recompute_source_ratings, hour=0, minute=0, unique=True, timeout=1800),
        # Сроки хранения персональных данных (/privacy); 03:30 по Москве.
        cron(purge_personal_data, hour=0, minute=30, unique=True, timeout=600),
        cron(run_watches, minute=set(range(4, 60, 10)), unique=True, timeout=3000),
        cron(follow_up_checks, minute={17, 47}, unique=True, timeout=1500),
    ]
    on_startup = startup
    on_shutdown = shutdown
    redis_settings = RedisSettings.from_dsn(get_settings().redis_url)
    max_jobs = 8
