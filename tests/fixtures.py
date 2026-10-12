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
