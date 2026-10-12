"""Консольные команды для разработки.

python -m app.cli seeds            # загрузить seeds/sources.yaml в реестр
python -m app.cli collect rss      # один проход сборщика RSS
python -m app.cli collect telegram # один проход по Telegram-каналам
python -m app.cli embed            # посчитать эмбеддинги (нужен образ воркера)
python -m app.cli stats            # метрики приёмки этапа 2
python -m app.cli ratings          # пересчитать рейтинг источников сейчас (обычно — ночью)
python -m app.cli stats --since 2026-10-12T15:00:00+03:00  # поток с начала замера
"""

import asyncio
import logging
import sys
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import text

from app.collectors.runner import poll_rss, poll_telegram, summarize
from app.collectors.seeds import load_seeds, read_seeds
from app.core.db import SessionLocal

STATS_SQL = {
    "документов всего": "SELECT count(*) FROM documents",
    "за 24 часа": (
        "SELECT count(*) FROM documents WHERE first_seen_at > now() - interval '24 hours'"
    ),
    "с эмбеддингом": "SELECT count(*) FROM documents WHERE embedding IS NOT NULL",
    "дубли текста у одного источника": """
        SELECT coalesce(sum(n - 1), 0) FROM (
            SELECT count(*) AS n FROM documents
            WHERE content_hash IS NOT NULL
            GROUP BY source_id, content_hash HAVING count(*) > 1
        ) d""",
    "версий (правок)": "SELECT count(*) FROM document_versions",
    "источников с ошибкой": "SELECT count(*) FROM sources WHERE consecutive_errors > 0",
}


async def _stats(since: str | None = None) -> None:
    async with SessionLocal() as session:
        for label, sql in STATS_SQL.items():
            print(f"{label}: {await session.scalar(text(sql))}")
        if since:
            start = datetime.fromisoformat(since)
            hours = (datetime.now(start.tzinfo or UTC) - start).total_seconds() / 3600
            rows = await session.execute(
                text(
                    "SELECT coalesce(s.handle_or_domain, '?'), count(*) FROM documents d "
                    "LEFT JOIN sources s ON s.id = d.source_id WHERE d.first_seen_at >= :t "
                    "GROUP BY 1 ORDER BY 2 DESC"
                ),
                {"t": start},
            )
            per_source = list(rows)
            total = sum(n for _, n in per_source)
            per_day = total / hours * 24 if hours > 0 else 0
            print(f"с {since}: {total} документов за {hours:.1f} ч ≈ {per_day:.0f} в сутки")
            for handle, n in per_source:
                print(f"    {handle}: {n}")
        rows = await session.execute(
            text(
                "SELECT handle_or_domain, consecutive_errors, last_error FROM sources "
                "WHERE consecutive_errors > 0 ORDER BY consecutive_errors DESC LIMIT 20"
            )
        )
        for handle, errors, last_error in rows:
            print(f"  ! {handle}: {errors} подряд — {last_error}")


async def _main(args: list[str]) -> None:
    match args:
        case ["seeds"]:
            async with SessionLocal() as session:
                added, updated = await load_seeds(session, read_seeds(Path("seeds/sources.yaml")))
            print(f"добавлено {added}, обновлено {updated}")
        case ["collect", "rss"]:
            print(summarize(await poll_rss(SessionLocal)))
        case ["collect", "telegram"]:
            print(summarize(await poll_telegram(SessionLocal)))
        case ["embed"]:
            from app.embeddings import SentenceTransformerEmbedder, embed_pending

            async with SessionLocal() as session:
                print(f"обработано: {await embed_pending(session, SentenceTransformerEmbedder())}")
        case ["ratings"]:
            from app.registry.service import recompute_ratings

            async with SessionLocal() as session:
                stats = await recompute_ratings(session)
                await session.commit()
            print(f"источников {stats.sources}, изменилось {stats.changed}")
        case ["stats"]:
            await _stats()
        case ["stats", "--since", since]:
            await _stats(since)
        case _:
            print(__doc__)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    asyncio.run(_main(sys.argv[1:]))
