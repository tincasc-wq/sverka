"""Скачивание поста Threads (threads.com / threads.net) по ссылке.

Threads — приложение Meta: обычным клиентам страница поста приходит пустой JS-заглушкой
(рендер в браузере + лента связанных веток), а серверный рендер с текстом одного поста
отдаёт только «известным краулерам» — поэтому качаем с UA facebookexternalhit
(проверены также Twitterbot и Googlebot, 03.11.2026). og:description содержит текст
именно того поста, на который ведёт ссылка, — «граница новости» соблюдена по построению.
Шерилки /share/… Threads сам редиректит на канонический /@автор/post/ID.

Время публикации серверно не отдаётся (в разметке только время сессии) —
published_at остаётся None, пайплайн опирается на first_seen_at.
"""

from urllib.parse import urlsplit

import httpx
from selectolax.lexbor import LexborHTMLParser as HTMLParser

from app.collectors.http import fetch, make_client
from app.collectors.store import DocumentIn
from app.collectors.urls import domain_of, normalize_url, without_query
from app.core.config import get_settings

THREADS_HOSTS = {"threads.com", "threads.net"}

# Meta отдаёт SSR только известным краулерам; короткий вариант «facebookexternalhit/1.1»
# тоже работает, полный — как в документации Meta.
CRAWLER_UA = "facebookexternalhit/1.1 (+http://www.facebook.com/externalhit_uatext.php)"


def _path_parts(url: str) -> list[str]:
    return [p for p in urlsplit(url).path.split("/") if p]


def is_threads(url: str) -> bool:
    return domain_of(url) in THREADS_HOSTS


def is_share_link(url: str) -> bool:
    parts = _path_parts(url)
    return is_threads(url) and bool(parts) and parts[0] == "share"


def post_handle(url: str) -> str | None:
    """@автор из ссылки на пост (…/@handle/post/<id>); для шерилок и профилей — None."""
    parts = _path_parts(url)
    if len(parts) == 3 and parts[0].startswith("@") and parts[1] == "post":
        return parts[0]
    return None


def parse_post(html: bytes | str, url: str, source_id: int | None) -> DocumentIn | None:
    """Текст одного поста из SSR-страницы; None — заглушка без поста (удалён/закрыт)."""
    tree = HTMLParser(html)
    # У поста og:title — «Автор (@handle) on Threads»; у заглушки его нет.
    if tree.css_first('meta[property="og:title"]') is None:
        return None
    description = tree.css_first('meta[property="og:description"]')
    text = (description.attributes.get("content") or "").strip() if description else ""
    return DocumentIn(
        url=normalize_url(without_query(url)),
        source_id=source_id,
        text=text or None,
        lang=None,  # <html lang="en"> у Threads — язык интерфейса, не поста
    )


def _proxy_candidates() -> list[str | None]:
    """Напрямую, затем через шлюз сборщика / TG-прокси (threads.com из РФ может не открываться)."""
    settings = get_settings()
    proxies: list[str | None] = [None]
    for proxy in (settings.collector_proxy, settings.telegram_proxy):
        if proxy and proxy not in proxies:
            proxies.append(proxy)
    return proxies


async def resolve_share(url: str) -> str:
    """Резолвит шерилку в финальный URL поста (Threads редиректит, follow_redirects)."""
    last: httpx.HTTPError | None = None
    for proxy in _proxy_candidates():
        try:
            async with make_client(public_only=True, proxy=proxy) as client:
                _, final = await fetch(client, url)
            return str(final)
        except httpx.HTTPError as exc:
            last = exc
    assert last is not None
    raise last


async def download(url: str, source_id: int | None) -> DocumentIn:
    """Пост по прямой ссылке или шерилке; сетевые сбои повторяем через прокси."""
    last: httpx.HTTPError | None = None
    for proxy in _proxy_candidates():
        try:
            async with make_client(public_only=True, proxy=proxy) as client:
                raw, final = await fetch(client, url, headers={"User-Agent": CRAWLER_UA})
            doc = parse_post(raw, str(final), source_id)
            if doc is None or not doc.text:
                raise ValueError(f"no text found in Threads post {url}")
            return doc
        except httpx.HTTPError as exc:
            last = exc
    assert last is not None
    raise last
