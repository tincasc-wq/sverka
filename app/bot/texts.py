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


def _rating_line(card: dict | None, seed_handle: str | None = None) -> str:
    if not card:
        who = f"{escape(seed_handle)} " if seed_handle else "Источника ссылки "
        return f"{who}нет в реестре источников — рейтинга нет."
    rating = card.get("rating")
    name = escape(card["name"])
    if not rating or not rating["shown"]:
        return f"Источник ссылки — {name}: недостаточно истории для рейтинга."
    low, high = rating["ci"]
    return f"Источник ссылки — {name}: рейтинг {rating['score']:g} из 100 ({low:g}–{high:g})."


def _link(item: dict) -> str:
    return f'<a href="{escape(item["url"])}">{escape(item["source"])}</a>'


def format_check(
    report: dict, seed_card: dict | None, report_url: str, seed_handle: str | None = None
) -> str:
    """Краткий итог для бота (ФТ-7.1): первоисточник, охват, ранние публикации, источник."""
    lines = [f"<b>{escape(report['seed']['title'] or 'Публикация')}</b>", ""]
    satire = report.get("satire")
    if satire:
        # Шутка важнее всего остального в ответе — первой строкой и без индекса.
        if satire["origin"]:
            lines += [f"🃏 <b>Похоже на шутку:</b> первоисточник — {escape(satire['source'])}.", ""]
        else:
            lines += [
                f"🃏 <b>Это сатирическое издание</b> ({escape(satire['source'])}): "
                "его материалы — вымысел по замыслу.",
                "",
            ]
    else:
        lines += _analysis_lines(report.get("analysis"))
    primary = report.get("primary")
    seed_entry = next((t for t in report["timeline"] if t.get("is_seed")), None)

    seed_cites = (seed_entry or {}).get("attributes_outside_archive") or []
    confirmed = primary and primary.get("membership") != "probable"
    primary_is_seed = primary and primary["url"] == report["seed"]["url"]
    if confirmed and not (primary_is_seed and seed_cites):
        lines.append(f"Первоисточник: {_link(primary)}, {_time(primary['time'])}")
    elif confirmed:
        # Самая ранняя в архиве — сама присланная, но она ссылается на кого-то ещё.
        lines.append(f"Самая ранняя публикация в архиве — эта ({_time(primary['time'])}).")
    elif primary:
        # Ранние публикации из полосы «вероятно то же событие» первоисточником не называем.
        root = primary.get("earliest_core_root")
        if not root or root["url"] == report["seed"]["url"]:
            lines.append("Более ранних публикаций именно об этом в архиве нет.")
        else:
            lines.append(f"Самая ранняя публикация именно об этом: {_link(root)}")
    if seed_cites:
        names = escape(", ".join(seed_cites))
        lines.append(
            f"Она ссылается на: {names}. Исходной публикации в архиве нет — "
            "первоисточник, вероятно, там."
        )

    core = report.get("core_publications", report["publications"])
    related = report["publications"] - core
    count = f"Публикаций именно об этом: {core}"
    if related:
        count += f" (и ещё {related} на ту же тему)"
    independent = report.get("independent_core", report["independent_reports"])
    lines.append(f"{count}; независимых групп источников: {independent}")

    early = [t for t in report["timeline"] if t["membership"] == "core"][:4]
    if len(early) > 1:
        lines += ["", "Хронология:"]
        for t in early:
            lines.append(
                f"• {_time(t['time'])} {escape(t['source'])} — {ROLE.get(t['role'], t['role'])}"
            )
    if report.get("earlier_related"):
        lines += ["", "Раньше по той же теме — то же событие или другое, без LLM не различить:"]
        for t in report["earlier_related"]:
            lines.append(f"• {_time(t['time'])} {escape(t['source'])}: {escape(t['title'] or '')}")

    lines += [
        "",
        _rating_line(seed_card, seed_handle),
        "",
        f"Полный отчёт: {escape(report_url)}",
        "",
        DISCLAIMER,
    ]
    return "\n".join(lines)


CLAIM_MARK = {
    "widely_confirmed": "✅",
    "partially_confirmed": "☑️",
    "not_confirmed": "❔",
    "refuted": "❌",
}


def _claim_line(claim: dict) -> str:
    supports = [
        e for e in claim["evidence"] if e["stance"] in ("supports", "refines") and e["independent"]
    ]
    contra = [
        e for e in claim["evidence"] if e["stance"] == "contradicts" and not e["low_confidence"]
    ]
    if contra:
        e = contra[0]
        detail = f" — противоречит {escape(e['source'])}"
        if e.get("discrepancy"):
            detail += f": {escape(e['discrepancy'])}"
    elif supports:
        detail = f" — подтверждают независимо: {len({e['source'] for e in supports})}"
    else:
        detail = " — независимых подтверждений нет"
    mark = CLAIM_MARK.get(claim.get("status"), "•")
    return f"{mark} {escape(claim['text'])}{detail}"


def _analysis_lines(analysis: dict | None) -> list[str]:
    if not analysis:
        return []
    if analysis.get("error"):
        return ["⚠️ Сверка утверждений не выполнена (языковая модель недоступна).", ""]
    if analysis.get("index") is None:
        return [f"Индекс подтверждённости: {escape(analysis['status_text'])}.", ""]
    status = escape(analysis["status_text"])
    lines = [f"<b>Индекс подтверждённости: {analysis['index']:g} из 100</b> — {status}"]
    checkable = [c for c in analysis["claims"] if c["checkable"]][:3]
    lines += [_claim_line(c) for c in checkable]
    return [*lines, ""]


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
