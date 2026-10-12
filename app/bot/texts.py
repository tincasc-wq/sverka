"""Тексты бота. HTML-разметка Telegram; всё, что пришло извне, экранируется."""

from datetime import datetime
from html import escape
from zoneinfo import ZoneInfo

MSK = ZoneInfo("Europe/Moscow")

DISCLAIMER = (
    "<i>Автоматический анализ открытых источников, не является утверждением "
    "об истинности или ложности.</i>"
)

START = (
    "Я проверяю, откуда взялась новость.\n\n"
    "Пришлите ссылку на статью или пост, перешлите сообщение из канала "
    "или вставьте текст — найду первоисточник, кто перепечатал и кто сообщил независимо.\n\n"
    "/source <code>tass.ru</code> или /source <code>@rbc_news</code> — карточка источника.\n\n"
    + DISCLAIMER
)

NOT_UNDERSTOOD = (
    "Не нашёл, что проверять. Пришлите ссылку, перешлите пост из канала "
    "или вставьте текст новости (от 40 символов)."
)
CHECKING = "🔎 Проверяю… Обычно это занимает до пары минут."
QUOTA = "Лимит бесплатных проверок на сегодня исчерпан ({limit} в сутки). Возвращайтесь завтра."
FAILED = "Не получилось проверить: {reason}"
FEEDBACK_ASK = "Опишите одним сообщением, что в отчёте не так — передам аналитику."
FEEDBACK_THANKS = "Спасибо, передал аналитику."
RECHECK_SCHEDULED = "Хорошо, проверю ещё раз через {hours:g} ч и пришлю результат."

ROLE = {"primary": "первоисточник", "derived": "перепечатка", "independent": "независимо"}


def _time(iso: str) -> str:
    return datetime.fromisoformat(iso).astimezone(MSK).strftime("%d.%m %H:%M")


def _rating_line(card: dict | None) -> str:
    if not card:
        return "Источник ссылки не в реестре."
    rating = card.get("rating")
    name = escape(card["name"])
    if not rating or not rating["shown"]:
        return f"Источник ссылки — {name}: недостаточно истории для рейтинга."
    low, high = rating["ci"]
    return f"Источник ссылки — {name}: рейтинг {rating['score']:g} из 100 ({low:g}–{high:g})."


def format_check(report: dict, seed_card: dict | None, report_url: str) -> str:
    """Краткий итог для бота (ФТ-7.1): первоисточник, охват, ранние публикации, источник."""
    lines = [f"<b>{escape(report['seed']['title'] or 'Публикация')}</b>", ""]
    primary = report.get("primary")
    if primary:
        mark = (
            " (принадлежность к событию не подтверждена)"
            if primary.get("membership") == "probable"
            else ""
        )
        lines.append(
            f'Первоисточник: <a href="{escape(primary["url"])}">{escape(primary["source"])}</a>, '
            f"{_time(primary['time'])}{mark}"
        )
        for note in primary.get("notes") or []:
            lines.append(f"⚠️ {escape(note)}")
    lines.append(
        f"Публикаций о событии: {report['publications']}, "
        f"независимых групп источников: {report['independent_reports']}"
    )
    early = [t for t in report["timeline"] if t["membership"] == "core"][:4]
    if len(early) > 1:
        lines.append("")
        lines.append("Хронология:")
        for t in early:
            lines.append(
                f"• {_time(t['time'])} {escape(t['source'])} — {ROLE.get(t['role'], t['role'])}"
            )
    lines += [
        "",
        _rating_line(seed_card),
        "",
        f"Полный отчёт: {escape(report_url)}",
        "",
        DISCLAIMER,
    ]
    return "\n".join(lines)


def format_text_matches(matches: list[dict], days: int) -> str:
    """Сценарий 3: где и когда текст появлялся раньше всего."""
    if not matches:
        return (
            f"За последние {days} дн. похожих публикаций в архиве не нашёл. "
            "Если есть ссылка на источник — пришлите её.\n\n" + DISCLAIMER
        )
    lines = [f"Похожие публикации за {days} дн., самые ранние сверху:", ""]
    for m in matches:
        lines.append(
            f"• {_time(m['time'])} {escape(m['source'])} — "
            f'<a href="{escape(m["url"])}">{escape(m["title"])}</a>'
        )
    lines += [
        "",
        "Чтобы построить полную хронологию, пришлите ссылку на одну из них.",
        "",
        DISCLAIMER,
    ]
    return "\n".join(lines)


def format_source_card(card: dict) -> str:
    lines = [f"<b>{escape(card['name'])}</b> ({escape(card['handle_or_domain'])})"]
    if card.get("owner"):
        lines.append(f"Владелец: {escape(card['owner'])}")
    lines.append(_rating_line(card).replace("Источник ссылки — ", "Рейтинг — "))
    if card.get("rating"):
        lines.append(f"Учтено исходов: {card['rating']['outcomes']}")
    lines.append(f"Публикаций в архиве: {card['documents_in_archive']}")
    flags = card.get("legal_flags") or {}
    if flags:
        lines.append("Отметки: " + escape(", ".join(f"{k}: {v}" for k, v in flags.items())))
    if card.get("history"):
        lines.append(f"Последнее изменение рейтинга: {escape(card['history'][0]['reason'])}")
    return "\n".join(lines)
