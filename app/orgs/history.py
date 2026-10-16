"""История проверок организации: поиск и фильтры, выгрузка CSV, месячный отчёт.

Отчёт проверки хранится JSON-текстом в Check.summary; нужные поля (заголовок, индекс,
статус, число публикаций) достаём прямо в SQL, чтобы не тянуть отчёты целиком.
"""

import csv
import io
from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import Float, Integer, case, cast, func, or_, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Check, Organization
from app.models.enums import CheckStatus
from app.orgs.service import MSK, month_start

CATEGORIES = {
    "ok": ("widely_confirmed", "partially_confirmed"),
    "open": ("not_confirmed", "no_independent", "too_early", "archive_gap", "no_checkable_claims"),
    "bad": ("disputed", "refuted", "questioned", "satire_origin", "satire_seed"),
}
CATEGORY_RU = {
    "ok": "подтверждено",
    "open": "не подтверждено или рано судить",
    "bad": "оспорено, опровергнуто или под вопросом",
    "failed": "проверка не удалась",
}
EXPORT_LIMIT = 20000

_report = case((Check.summary.like("{%"), cast(Check.summary, JSONB)), else_=None)


def _field(*path: str):
    return func.jsonb_extract_path_text(_report, *path)


TITLE = _field("seed", "title")
STATUS = _field("analysis", "status")
STATUS_TEXT = _field("analysis", "status_text")
INDEX = cast(_field("analysis", "index"), Float)
PUBLICATIONS = cast(func.coalesce(_field("core_publications"), _field("publications")), Integer)


def category_of(check_status: str, status: str | None) -> str | None:
    if check_status == CheckStatus.FAILED:
        return "failed"
    return next((k for k, codes in CATEGORIES.items() if status in codes), None)


@dataclass
class HistoryFilter:
    q: str = ""
    member: int | None = None  # user_id сотрудника
    category: str = ""
    month: str = ""  # ГГГГ-ММ

    @classmethod
    def from_params(cls, params) -> "HistoryFilter":
        member = params.get("member") or ""
        category = params.get("category") or ""
        return cls(
            q=(params.get("q") or "").strip()[:200],
            member=int(member) if member.isdigit() else None,
            category=category if category in CATEGORY_RU else "",
            month=month if _month_bounds(month := params.get("month") or "") else "",
        )

    @property
    def active(self) -> bool:
        return bool(self.q or self.member or self.category or self.month)

    def query(self) -> str:
        from urllib.parse import urlencode

        pairs = {"q": self.q, "member": self.member or "", "category": self.category}
        pairs["month"] = self.month
        return urlencode({k: v for k, v in pairs.items() if v})


def _month_bounds(month: str) -> tuple[datetime, datetime] | None:
    try:
        start = datetime.strptime(month, "%Y-%m").replace(tzinfo=MSK)
    except ValueError:
        return None
    end = (
        start.replace(year=start.year + 1, month=1)
        if start.month == 12
        else start.replace(month=start.month + 1)
    )
    return start, end


def recent_months(count: int = 12) -> list[str]:
    start = month_start()
    months = []
    for _ in range(count):
        months.append(start.strftime("%Y-%m"))
        start = (
            start.replace(year=start.year - 1, month=12)
            if start.month == 1
            else start.replace(month=start.month - 1)
        )
    return months


@dataclass
class Row:
    id: int
    created_at: datetime
    user_id: int | None
    token: str | None
    check_status: str
    input_raw: str
    title: str | None
    index: float | None
    status: str | None
    status_text: str | None
    publications: int | None
    category: str | None = field(init=False)

    def __post_init__(self) -> None:
        self.category = category_of(self.check_status, self.status)

    @property
    def label(self) -> str:
        return (self.title or self.input_raw or "")[:300]


def _base(org: Organization):
    return select(
        Check.id,
        Check.created_at,
        Check.user_id,
        Check.public_token,
        Check.status,
        Check.input_raw,
        TITLE,
        INDEX,
        STATUS,
        STATUS_TEXT,
        PUBLICATIONS,
    ).where(Check.org_id == org.id, Check.channel.is_distinct_from("auto"))


def _filtered(org: Organization, f: HistoryFilter):
    stmt = _base(org)
    if f.q:
        like = f"%{f.q}%"
        stmt = stmt.where(or_(Check.input_raw.ilike(like), TITLE.ilike(like)))
    if f.member:
        stmt = stmt.where(Check.user_id == f.member)
    if f.month and (bounds := _month_bounds(f.month)):
        stmt = stmt.where(Check.created_at >= bounds[0], Check.created_at < bounds[1])
    if f.category == "failed":
        stmt = stmt.where(Check.status == CheckStatus.FAILED)
    elif f.category:
        stmt = stmt.where(STATUS.in_(CATEGORIES[f.category]))
    return stmt


async def history(
    session: AsyncSession, org: Organization, f: HistoryFilter, limit: int = 100
) -> list[Row]:
    rows = await session.execute(_filtered(org, f).order_by(Check.id.desc()).limit(limit))
    return [Row(*r) for r in rows]


def csv_bytes(rows: list[Row], authors: dict[int, str], base_url: str) -> bytes:
    """CSV для Excel: UTF-8 с BOM, разделитель «;»."""
    from app.web.report import msk

    out = io.StringIO()
    writer = csv.writer(out, delimiter=";")
    writer.writerow(
        [
            "Дата (МСК)",
            "Сотрудник",
            "Что проверяли",
            "Индекс",
            "Итог",
            "Категория",
            "Публикаций",
            "Отчёт",
        ]
    )
    for r in rows:
        writer.writerow(
            [
                msk(r.created_at),
                authors.get(r.user_id, "бывший сотрудник"),
                r.label,
                "" if r.index is None else f"{r.index:g}".replace(".", ","),
                r.status_text or ("не удалось" if r.check_status == "failed" else ""),
                CATEGORY_RU.get(r.category, ""),
                "" if r.publications is None else r.publications,
                f"{base_url}/r/{r.token}" if r.token else "",
            ]
        )
    return ("﻿" + out.getvalue()).encode("utf-8")


async def month_summary(
    session: AsyncSession, org: Organization, month: str, authors: dict[int, str]
) -> dict:
    """Сводка за месяц для PDF: итоги, по сотрудникам, по категориям, спорные новости."""
    rows = await history(session, org, HistoryFilter(month=month), limit=EXPORT_LIMIT)
    by_member: dict[str, int] = {}
    by_category = dict.fromkeys(CATEGORY_RU, 0)
    for r in rows:
        name = authors.get(r.user_id, "бывший сотрудник")
        by_member[name] = by_member.get(name, 0) + 1
        if r.category:
            by_category[r.category] += 1
    indexes = [r.index for r in rows if r.index is not None]
    done = [r for r in rows if r.check_status == CheckStatus.DONE and r.title]
    seen: set[str] = set()
    top = []
    for r in sorted(done, key=lambda r: -(r.publications or 0)):
        if r.title not in seen:
            seen.add(r.title)
            top.append(r)
        if len(top) == 10:
            break
    return {
        "month": month,
        "total": len(rows),
        "done": sum(1 for r in rows if r.check_status == CheckStatus.DONE),
        "by_member": sorted(by_member.items(), key=lambda x: -x[1]),
        "by_category": by_category,
        "avg_index": round(sum(indexes) / len(indexes), 1) if indexes else None,
        "bad": [r for r in rows if r.category == "bad"][:40],
        "top": top,
    }
