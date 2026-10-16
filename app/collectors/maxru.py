"""Посты мессенджера Max (max.ru/<канал>/<id поста>): только по ссылке, без сбора каналов.

Ленты канала без входа у Max нет (в отличие от t.me/s), а страница отдельного поста
открывается: название канала в h1.title, сам пост — в div.description. Даты публикации
на странице нет — проверка берёт время проверки и помечает это в отчёте.

Max ограничивает частые запросы с одного адреса и вместо поста отдаёт общую заглушку
(«MAX — быстрое и легкое приложение…», «Не нашли чат по этой ссылке»): такую страницу
нельзя проверять как новость — повторяем через шлюз, а затем честно сообщаем об ошибке.
"""

import re

from selectolax.lexbor import LexborHTMLParser as HTMLParser

from app.collectors.store import DocumentIn

MAX_HOST = "max.ru"
_POST = re.compile(r"^https?://(?:www\.)?max\.ru/([^/?#\s]+)/([^/?#\s]+)", re.IGNORECASE)
TITLE_CHARS = 140


class MaxStub(ValueError):
    """Max не отдал пост: заглушка вместо страницы (ограничение по адресу или неверная ссылка)."""


def is_max_post(url: str) -> bool:
    return bool(_POST.match(url))


def channel_handle(url: str) -> str | None:
    """Ключ канала в реестре: max.ru/<канал> в нижнем регистре (Max адреса не различает)."""
    match = re.match(r"^https?://(?:www\.)?max\.ru/([^/?#\s]+)", url, re.IGNORECASE)
    return f"{MAX_HOST}/{match.group(1).lower()}" if match else None


def _post_text(node) -> str:
    html = re.sub(r"<br\s*/?>", "\n", node.html or "", flags=re.IGNORECASE)
    text = HTMLParser(html).text(separator="")
    lines = [re.sub(r"[ \t ]+", " ", line).strip() for line in text.splitlines()]
    return "\n".join(line for line in lines if line).strip()


def title_from(text: str) -> str:
    """Заголовок поста — первая фраза, а не название канала, как в <title> у Max."""
    first = text.split("\n", 1)[0]
    sentence = re.split(r"(?<=[.!?…])\s", first, maxsplit=1)[0]
    if len(sentence) <= TITLE_CHARS:
        return sentence
    return sentence[: TITLE_CHARS - 1].rsplit(" ", 1)[0] + "…"


def parse_post(html: bytes | str, url: str, source_id: int | None) -> DocumentIn:
    tree = HTMLParser(html)
    node = tree.css_first("div.card div.description") or tree.css_first("div.description")
    text = _post_text(node) if node else ""
    if not text:
        raise MaxStub(f"Max не отдал пост {url}")
    return DocumentIn(
        url=url,
        source_id=source_id,
        title=title_from(text),
        text=text,
        published_at=None,  # на странице поста Max даты нет
    )
