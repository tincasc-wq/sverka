"""Статистика для админки: пользователи, проверки по каналам, качество, расходы, сбор.

Дни — по Москве. Служебные проверки (channel = service: прогоны качества, калибровка)
в пользователей и долю успешных не входят.
"""

import json

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

USD_RUB = 90

DAILY = text("""
with days as (
  select generate_series(
    (now() at time zone 'Europe/Moscow')::date - (:days - 1),
    (now() at time zone 'Europe/Moscow')::date,
    interval '1 day')::date as day
),
c as (
  select (created_at at time zone 'Europe/Moscow')::date as day, user_id, channel, status,
         extract(epoch from (finished_at - created_at)) as secs
  from checks
  where created_at >= now() - make_interval(days => :days + 1)
),
u as (
  select (created_at at time zone 'Europe/Moscow')::date as day, count(*) as n
  from users where created_at >= now() - make_interval(days => :days + 1) group by 1
),
l as (
  select (created_at at time zone 'Europe/Moscow')::date as day, sum(cost_usd) as usd
  from llm_calls where created_at >= now() - make_interval(days => :days + 1) group by 1
),
d as (
  select (first_seen_at at time zone 'Europe/Moscow')::date as day, count(*) as n
  from documents where first_seen_at >= now() - make_interval(days => :days + 1)
    and url not like 'user-text://%' group by 1
)
select days.day,
  count(distinct c.user_id) filter (where c.channel is distinct from 'service') as users,
  coalesce(max(u.n), 0) as new_users,
  count(c.*) filter (where c.channel = 'bot') as bot,
  count(c.*) filter (where c.channel = 'web') as web,
  count(c.*) filter (where c.channel = 'service') as service,
  count(c.*) filter (where c.channel is distinct from 'service' and c.status = 'failed') as failed,
  count(c.*) filter (where c.channel is distinct from 'service' and c.status = 'done') as done,
  percentile_cont(0.5) within group (order by c.secs)
    filter (where c.channel is distinct from 'service' and c.status = 'done') as p50,
  percentile_cont(0.9) within group (order by c.secs)
    filter (where c.channel is distinct from 'service' and c.status = 'done') as p90,
  coalesce(max(l.usd), 0) * :rub as llm_rub,
  coalesce(max(d.n), 0) as documents
from days
left join c on c.day = days.day
left join u on u.day = days.day
left join l on l.day = days.day
left join d on d.day = days.day
group by days.day
order by days.day desc
""")

TOTALS = text("""
select
  (select count(*) from users) as users,
  (select count(*) from users where tg_id is not null) as tg,
  (select count(*) from users where email is not null) as email,
  (select count(*) from users where tg_id is not null and email is not null) as both_,
  (select count(*) from users where tg_id is null and email is null) as anonymous,
  (select count(*) from organizations) as orgs,
  (select count(*) from checks where channel is distinct from 'service') as checks,
  (select count(*) from documents where url not like 'user-text://%') as documents,
  (select count(*) from sources where is_tracked) as sources
""")

FAILED = text("""
select c.created_at, c.channel, c.input_raw, c.summary, c.public_token
from checks c
where c.status = 'failed' and c.channel is distinct from 'service'
order by c.id desc limit 10
""")

BROKEN_SOURCES = text("""
select id, name, consecutive_errors, last_error, last_polled_at
from sources
where is_tracked and consecutive_errors > 0
order by consecutive_errors desc, name limit 15
""")


async def collect(session: AsyncSession, days: int = 14) -> dict:
    daily = [dict(r._mapping) for r in await session.execute(DAILY, {"days": days, "rub": USD_RUB})]
    for row in daily:
        finished = row["done"] + row["failed"]
        row["success"] = round(100 * row["done"] / finished) if finished else None
        row["llm_rub"] = round(float(row["llm_rub"]), 2)
    totals = dict((await session.execute(TOTALS)).one()._mapping)
    failed = []
    for r in await session.execute(FAILED):
        try:
            reason = json.loads(r.summary or "{}").get("error")
        except ValueError:
            reason = None
        failed.append({**dict(r._mapping), "reason": reason})
    broken = [dict(r._mapping) for r in await session.execute(BROKEN_SOURCES)]
    from app.embeddings import BACKLOG_ALERT, embedding_backlog

    waiting, oldest = await embedding_backlog(session)
    backlog = {"waiting": waiting, "oldest": oldest, "alert": waiting > BACKLOG_ALERT}
    return {
        "daily": daily,
        "totals": totals,
        "failed": failed,
        "broken": broken,
        "days": days,
        "backlog": backlog,
    }
