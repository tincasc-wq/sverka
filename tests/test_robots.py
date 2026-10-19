import httpx

from app.collectors import robots
from app.collectors.robots import parse

REUTERS = """
User-agent: Googlebot
Allow: /

User-agent: *
Disallow: /pf/api/
Disallow: /
"""

LENTA = """
User-agent: Yandex
Disallow: /
User-agent: *
Disallow: /search
Disallow: */2020/02/13/money/
Disallow: /?
"""


def test_reuters_closed_for_other_bots():
    rules = parse(REUTERS)
    assert not rules.allows(
        "https://www.reuters.com/arc/outboundfeeds/news-sitemap/?outputType=xml"
    )
    assert parse(REUTERS, bot="googlebot").allows("https://www.reuters.com/world/")


def test_wildcards_and_longest_match():
    # Стандартный urllib.robotparser путал такие шаблоны и «запрещал» ленту Ленты.ру.
    rules = parse(LENTA)
    assert rules.allows("https://lenta.ru/rss/news")
    assert rules.allows("https://lenta.ru/news/2026/10/20/kallas/")
    assert not rules.allows("https://lenta.ru/search?q=x")
    assert not rules.allows("https://lenta.ru/?utm=1")
    assert not rules.allows("https://lenta.ru/news/2020/02/13/money/")
    mixed = parse("User-agent: *\nDisallow: /news\nAllow: /news/rss$\n")
    assert mixed.allows("https://x.ru/news/rss")
    assert not mixed.allows("https://x.ru/news/rss2")
    assert not mixed.allows("https://x.ru/news/1")


def test_own_group_wins_over_star():
    text = "User-agent: *\nDisallow: /\n\nUser-agent: SverkaBot\nAllow: /\n"
    assert parse(text).allows("https://x.ru/feed")
    assert parse("User-agent: *\nDisallow:\n").allows("https://x.ru/feed")  # пустой Disallow


async def test_note_for_registry():
    robots._cache.clear()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "closed.ru":
            return httpx.Response(200, text="User-agent: *\nDisallow: /\n")
        if request.url.host == "down.ru":
            return httpx.Response(503)
        return httpx.Response(404)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        assert await robots.note(client, "https://open.ru/rss", "https://open.ru/a") is None
        note = await robots.note(client, "https://closed.ru/rss", "https://closed.ru/a")
        assert note == "robots.txt запрещает: лента, статьи"
        assert await robots.note(client, "https://down.ru/rss", None) == ""  # неизвестно
    robots._cache.clear()
