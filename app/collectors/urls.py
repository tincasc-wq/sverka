"""Нормализация URL (ФТ-1.3): одна публикация — один адрес в архиве."""

import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

# Метки рекламы и аналитики, не меняющие содержимое страницы.
_TRACKING_PARAMS = {
    "fbclid",
    "gclid",
    "yclid",
    "ysclid",
    "mc_cid",
    "mc_eid",
    "from",
    "ref",
    "_openstat",
    "amp",
    "utm_referrer",
    "rss",
    "partner",
    "rcmclid",  # рекомендательный виджет (Lenta.ru и др.)
    "erid",  # маркировка рекламы
    "_ga",
    "dclid",
    "gclsrc",
    "igshid",
    "smid",
    "ref_src",
    "ref_url",
    "share",
    "xmt",  # метка шерилки Threads
    "slof",
    "hl",  # язык интерфейса (Google/Threads), не содержимого
}
_AMP_PATH = re.compile(r"/amp/?$|^/amp(?=/)")


def normalize_url(url: str) -> str:
    parts = urlsplit(url.strip())
    scheme = "https" if parts.scheme in ("http", "https", "") else parts.scheme
    host = (parts.hostname or "").lower().removeprefix("www.")
    if parts.port and parts.port not in (80, 443):
        host = f"{host}:{parts.port}"

    path = _AMP_PATH.sub("", parts.path) or "/"
    if len(path) > 1:
        path = path.rstrip("/")

    query = [
        (k, v)
        for k, v in parse_qsl(parts.query, keep_blank_values=True)
        if not k.lower().startswith("utm_") and k.lower() not in _TRACKING_PARAMS
    ]
    query.sort()
    return urlunsplit((scheme, host, path, urlencode(query), ""))


def domain_of(url: str) -> str:
    return (urlsplit(url).hostname or "").lower().removeprefix("www.")


def without_query(url: str) -> str:
    """Адрес без параметров — запасной ключ поиска в архиве, когда в ссылке неизвестная метка."""
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))
