"""Запись собранных публикаций в архив с дедупликацией и учётом правок (ФТ-3.2, ФТ-3.3)."""

from dataclasses import dataclass, field
from datetime import date, datetime
from enum import StrEnum

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.collectors.fingerprint import content_hash, simhash
from app.collectors.urls import domain_of, normalize_url
from app.models import Document, DocumentVersion
from app.models.enums import ChangeType


@dataclass
class DocumentIn:
    url: str
    source_id: int | None = None
    title: str | None = None
    text: str | None = None
    published_at: datetime | None = None
    lang: str | None = "ru"
    stated_date: date | None = None
    links: list[str] = field(default_factory=list)


class SaveResult(StrEnum):
    INSERTED = "inserted"
    UNCHANGED = "unchanged"  # тот же URL, текст не изменился
    EDITED = "edited"  # тот же URL, текст изменился — сохранена версия
    DUPLICATE = "duplicate"  # тот же текст у того же источника под другим URL


def _body(doc: DocumentIn) -> str:
    return "\n".join(part for part in (doc.title, doc.text) if part)


def external_links(doc_url: str, links: list[str]) -> list[str]:
    """Ссылки на другие сайты и каналы; внутренние («читайте также») для графа — шум."""
    own = domain_of(doc_url)
    own_channel = doc_url.split("/")[3] if own == "t.me" else None
    result: list[str] = []
    for link in links:
        if not link.startswith(("http://", "https://")):
            continue
        url = normalize_url(link)
        if domain_of(url) == own and (own != "t.me" or url.split("/")[3] == own_channel):
            continue
        if url not in result:
            result.append(url)
    return result


async def save_document(session: AsyncSession, doc: DocumentIn) -> SaveResult:
    url = normalize_url(doc.url)
    body = _body(doc)
    chash = content_hash(body) if body else None

    existing = await session.scalar(select(Document).where(Document.url == url))
    if existing is not None:
        if not chash or chash == existing.content_hash:
            return SaveResult.UNCHANGED
        # Издание поправило публикацию: старый текст уходит в историю версий.
        session.add(
            DocumentVersion(
                document_id=existing.id, text=existing.text, change_type=ChangeType.EDIT
            )
        )
        existing.title = doc.title or existing.title
        existing.text = doc.text or existing.text
        existing.content_hash = chash
        existing.simhash = simhash(body)
        existing.embedding = None  # пересчитается фоновой задачей
        await session.flush()
        return SaveResult.EDITED

    if chash and doc.source_id is not None:
        same_text = await session.scalar(
            select(Document.id).where(
                Document.source_id == doc.source_id, Document.content_hash == chash
            )
        )
        if same_text is not None:
            return SaveResult.DUPLICATE

    stmt = (
        insert(Document)
        .values(
            url=url,
            source_id=doc.source_id,
            title=doc.title,
            text=doc.text,
            lang=doc.lang,
            published_at=doc.published_at,
            stated_date=doc.stated_date,
            links=external_links(url, doc.links),
            content_hash=chash,
            simhash=simhash(body) if body else None,
        )
        .on_conflict_do_nothing(index_elements=["url"])
        .returning(Document.id)
    )
    inserted = await session.scalar(stmt)
    # Параллельный сборщик мог вставить тот же URL между проверкой и вставкой.
    return SaveResult.INSERTED if inserted is not None else SaveResult.UNCHANGED


async def known_urls(session: AsyncSession, urls: list[str]) -> set[str]:
    normalized = [normalize_url(u) for u in urls]
    rows = await session.scalars(select(Document.url).where(Document.url.in_(normalized)))
    return set(rows)
