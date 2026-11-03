"""Образцы ответов внешних источников для тестов без сети."""

RSS_FEED = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel><title>Тестовое СМИ</title>
<item>
  <title>На складе в Подмосковье произошёл пожар</title>
  <link>https://example.ru/news/1?utm_source=rss</link>
  <description>&lt;p&gt;Кратко: горит склад.&lt;/p&gt;</description>
  <pubDate>Mon, 12 Oct 2026 10:15:00 +0300</pubDate>
</item>
<item>
  <title>ЦБ сохранил ключевую ставку</title>
  <link>https://example.ru/news/2</link>
  <description>Решение совета директоров.</description>
  <pubDate>Mon, 12 Oct 2026 11:00:00 +0300</pubDate>
</item>
</channel></rss>
""".encode()

ARTICLE_HTML = """<!doctype html><html><head><title>Пожар</title></head><body>
<nav>Главная | Новости | Спорт</nav>
<article>
<h1>На складе в Подмосковье произошёл пожар</h1>
<p>Склад площадью 2000 квадратных метров загорелся утром в понедельник в Подольске.</p>
<p>На месте работают 40 пожарных и 12 единиц техники, сообщили в ГУ МЧС по Московской области.</p>
<p>Пострадавших нет, причины возгорания устанавливаются. Возбуждено дело о нарушении правил
пожарной безопасности, сообщили в прокуратуре.</p>
</article>
<footer>© Тестовое СМИ. Реклама.</footer>
</body></html>
""".encode()

TELEGRAM_PAGE = """<html><body><section class="tgme_channel_history">
<div class="tgme_widget_message_wrap"><div class="tgme_widget_message" data-post="testchan/101">
  <div class="tgme_widget_message_text">Срочно: пожар на складе в Подольске.<br/>
  Работают 40 пожарных.</div>
  <a class="tgme_widget_message_date"><time datetime="2026-10-12T07:20:00+00:00">10:20</time></a>
</div></div>
<div class="tgme_widget_message_wrap"><div class="tgme_widget_message" data-post="testchan/102">
  <a class="tgme_widget_message_photo_wrap"></a>
  <a class="tgme_widget_message_date"><time datetime="2026-10-12T07:25:00+00:00">10:25</time></a>
</div></div>
</section></body></html>
""".encode()

# Threads отдаёт пост серверно только «известным краулерам» (facebookexternalhit и т.п.):
# og:description содержит текст одного поста, og:title — «Автор (@handle) on Threads».
# Время публикации в разметке нет. Образец — реальный пост @newslabenzo/DfP_J88Dbbz (03.11.2026).
# HTML-сущности как в оригинале: og:description приходит в &#xNN; и декодируется парсером.
_THREADS_TEXT = (
    "&#x41f;&#x43e;&#x434;&#x43f;&#x438;&#x441;&#x44b;"
    "&#x432;&#x430;&#x439;&#x441;&#x44f; "
    "&#x43d;&#x430; LABENZO NEWS, "
    "&#x447;&#x442;&#x43e;&#x431;&#x44b; "
    "&#x431;&#x44b;&#x442;&#x44c; &#x432; "
    "&#x43a;&#x443;&#x440;&#x441;&#x435; "
    "&#x433;&#x43b;&#x430;&#x432;&#x43d;&#x43e;&#x433;&#x43e;: "
    "https://t.me/labenzonews/4445"
)
THREADS_POST_PAGE = (
    b'<!doctype html><html lang="en" dir="ltr"><head>'
    b'<meta property="og:site_name" content="Threads" />'
    b'<meta property="og:type" content="article" />'
    b'<meta property="og:title" content="LABENZO NEWS (&#064;newslabenzo) on Threads" />'
    + f'<meta property="og:description" content="{_THREADS_TEXT}" />'.encode()
    + f'<meta name="twitter:description" content="{_THREADS_TEXT}" />'.encode()
    + f"<title>{_THREADS_TEXT}</title>".encode()
    + b'</head><body><div id="root"></div></body></html>'
)

# То, что Threads отдаёт обычным клиентам (включая наш дефолтный UA): пустая JS-заглушка.
THREADS_SHELL_PAGE = b"""<!doctype html><html lang="en" dir="ltr"><head>
<title>Threads</title>
</head><body><div id="root"></div></body></html>
"""
