"""Поправки — эталонный случай Minerva Zen (14.10.2026).

10.10 Шарий: танкер Minerva Zen везёт в США первую партию российского дизеля. Многие
повторили, ссылаясь на «данные источников». 12.10 отслеживающая танкеры компания и
оператор судна: на борту жидкое удобрение (КАС), не дизель. «Оперативная линия» написала
об этом в шуточной форме. Сервис должен сказать «оспорено», а не «подтверждено».
"""

import json
from datetime import UTC, datetime, timedelta

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot import texts
from app.check import run_check
from app.core.scoring import IndexConfig
from app.followups import follow_up, news_of_correction
from app.models import Check, Document, Source, User
from app.pipeline.index import Evidence, score_claim
from app.pipeline.story import distinctive_names
from app.plans import checks_in_period
from tests.test_embeddings import FakeEmbedder
from tests.test_llm import answer, client
from tests.test_story import _doc, vec

CFG = IndexConfig()
CLAIM = "Танкер Minerva Zen везёт в США 280 тысяч баррелей российского дизельного топлива"

SEED = (
    "Первая партия российского дизеля, перевозимая на борту судна MINERVA ZEN, приближается "
    "к восточному побережью США. На борту 280 000 баррелей дизельного топлива."
)
RETELL_1 = "Как сообщают СМИ, танкер Minerva Zen везёт в США российское дизельное топливо."
RETELL_2 = "По данным источников, судно Minerva Zen доставит в США первую партию дизеля из России."
OWN = "По данным Vortexa, танкер Minerva Zen перевозит около 280 тысяч баррелей дизельного топлива."
CORRECTION = (
    "TankerTrackers: Minerva Zen is NOT carrying Russian diesel. She is bringing in UAN, "
    "a liquid form of fertilizer, the vessel operator confirmed."
)
JOKE = (
    "Капитан парохода Minerva Zen сообщил страждущим, что привёз он им из Питера не солярку, "
    "а жидкое азотное удобрение - карбамидно-аммиачную смесь."
)


def model(request: httpx.Request) -> httpx.Response:
    """Модель по содержимому публикации: пересказ, свой источник, поправка."""
    user = json.loads(request.content)["messages"][1]["content"]
    if "Кандидаты:" in user:
        # То же событие — кандидаты про этот танкер (их нашли по имени, а не по близости).
        docs = user.split("Кандидаты:")[1].split("<document id=")[1:]
        return answer(
            {
                "decisions": [
                    {"id": i, "same_event": "Minerva" in d, "reason": "тот же танкер"}
                    for i, d in enumerate(docs, 1)
                ]
            }
        )
    if "<claims>" not in user:
        return answer({"claims": [{"text": CLAIM}]})
    doc = user.split("<document>")[1]
    if "UAN" in doc:
        r = {
            "stance": "corrects",
            "sourcing": "own",
            "both_can_be_true": False,
            "quote": "Minerva Zen is NOT carrying Russian diesel",
            "discrepancy": "жидкое удобрение, а не дизельное топливо",
        }
    elif "карбамидно" in doc:
        r = {
            "stance": "corrects",
            "sourcing": "retelling",
            "both_can_be_true": False,
            "quote": "привёз он им из Питера не солярку, а жидкое азотное удобрение",
            "discrepancy": "удобрение, а не солярка",
        }
    elif "Vortexa" in doc:
        r = {
            "stance": "supports",
            "sourcing": "own",
            "quote": "танкер Minerva Zen перевозит около 280 тысяч баррелей дизельного топлива",
        }
    else:
        quote = (
            "танкер Minerva Zen везёт в США российское дизельное топливо"
            if "СМИ" in doc
            else "судно Minerva Zen доставит в США первую партию дизеля из России"
        )
        r = {"stance": "supports", "sourcing": "retelling", "quote": quote}
    return answer({"results": [{"claim": 1, **r}]})


async def _story(session: AsyncSession, with_correction: bool) -> tuple[Document, list[Source]]:
    names = [
        ("Анатолий Шарий", "tg", "@ASupersharij"),
        ("Издание А", "media", "a.test"),
        ("Издание Б", "media", "b.test"),
        ("Lloyd's List", "media", "lloyds.test"),
        ("The Defense News", "media", "defense.test"),
        ("Оперативная линия", "tg", "@op"),
    ]
    s = []
    for name, kind, handle in names:
        src = Source(type=kind, name=name, handle_or_domain=handle)
        session.add(src)
        s.append(src)
    await session.flush()
    seed = await _doc(session, "https://t.me/ASupersharij/61137", 1.0, 0, s[0], SEED)
    await _doc(session, "https://a.test/1", 0.95, 60, s[1], RETELL_1)
    await _doc(session, "https://b.test/1", 0.95, 90, s[2], RETELL_2)
    await _doc(session, "https://lloyds.test/1", 0.94, 120, s[3], OWN)
    if with_correction:
        await _doc(session, "https://defense.test/1", 0.93, 2880, s[4], CORRECTION)
        await _doc(session, "https://t.me/op/47128", 0.93, 3000, s[5], JOKE)
    return seed, s


def test_correction_outweighs_any_number_of_repeats() -> None:
    def ev(group: str, stance: str, sourcing: str | None = "own") -> Evidence:
        return Evidence(group, "media", 50, stance, True, sourcing)

    repeats = [ev(f"g{i}", "supports") for i in range(10)]
    confirmed = score_claim(repeats, 50, CFG)
    corrected = score_claim([*repeats, ev("tracker", "corrects")], 50, CFG)
    assert confirmed.status == "widely_confirmed"
    assert corrected.status == "disputed" and corrected.index <= CFG.correction_cap

    # Одна поправка из Telegram — «под вопросом»; вторая независимая — уже «оспорено».
    def tg(group: str) -> Evidence:
        return Evidence(group, "tg", 50, "corrects", True, "own")

    weak = score_claim([*repeats, tg("channel")], 50, CFG)
    assert weak.status == "questioned" and weak.index <= CFG.weak_correction_cap
    assert score_claim([*repeats, tg("channel"), tg("other")], 50, CFG).status == "disputed"

    # Пересказы без своего источника подтверждением не считаются.
    retold = score_claim([ev(f"r{i}", "supports", "retelling") for i in range(10)], 50, CFG)
    assert retold.independent_groups == {} and retold.status == "not_confirmed"


async def test_minerva_zen_is_disputed_not_confirmed(session: AsyncSession) -> None:
    seed, _ = await _story(session, with_correction=True)
    check = await run_check(session, f"doc:{seed.id}", FakeEmbedder(), llm=client(model))
    analysis = json.loads(check.summary)["analysis"]

    assert analysis["status"] == "disputed" and analysis["index"] <= CFG.correction_cap
    assert analysis["status_text"].startswith("оспорено")
    [claim] = analysis["claims"]
    by_source = {e["source"]: e for e in claim["evidence"]}
    assert by_source["The Defense News"]["stance"] == "corrects"
    assert by_source["Издание А"]["sourcing"] == "retelling"
    assert claim["independent_groups"] == 3  # Lloyd's List, две поправки; пересказы — нет
    sources = [c["source"] for c in analysis["corrections"]]
    assert sources == ["The Defense News", "Оперативная линия"]  # по времени

    text = texts.format_check(json.loads(check.summary), None, "http://x/r/1")
    assert "Позднее вышла поправка" in text and "The Defense News" in text
    assert "поправка от" in text


async def test_follow_up_notifies_who_checked_before_the_correction(
    session: AsyncSession,
) -> None:
    seed, sources = await _story(session, with_correction=False)
    user = User(tg_id=4242, plan="pro")
    session.add(user)
    await session.flush()
    origin = await run_check(
        session,
        f"doc:{seed.id}",
        FakeEmbedder(),
        llm=client(model),
        user_id=user.id,
        channel="bot",
        input_raw=seed.url,
    )
    origin.finished_at = datetime.now(UTC) - timedelta(hours=7)
    await session.flush()
    before = json.loads(origin.summary)["analysis"]
    assert before["status"] != "disputed" and not before["corrections"]

    class Bot:
        sent: list[tuple[int, str]] = []

        async def send_message(self, chat_id, text, **_):
            self.sent.append((chat_id, text))

    bot = Bot()
    # Через двое суток в архиве — поправка; близка к новости, значит войдёт в её историю.
    later = Document(
        url="https://defense.test/1",
        text=CORRECTION,
        embedding=vec(0.93),
        published_at=seed.published_at + timedelta(days=2),
        source_id=sources[4].id,
    )
    session.add(later)
    await session.flush()

    outcome = await follow_up(session, origin, FakeEmbedder(), client(model), bot)

    assert outcome == "corrected"
    [(chat, message)] = bot.sent
    assert chat == 4242 and "Поправка к новости" in message and "Обновлённый отчёт" in message
    origin = await session.get(Check, origin.id)
    assert json.loads(origin.summary)["followup"]["status_text"].startswith("оспорено")
    assert origin.auto_runs == 1
    # Автоперепроверка — за счёт сервиса: в лимит пользователя идёт только его проверка.
    assert await checks_in_period(session, user, "month") == 1

    # Повторно до AUTO_EVERY не смотрим, а без новых публикаций модель не трогаем.
    assert await follow_up(session, origin, FakeEmbedder(), client(model), bot) == "quiet"


def test_only_new_corrections_are_news() -> None:
    old = {"status": "disputed", "corrections": [{"url": "u1"}]}
    assert news_of_correction(old, {"status": "disputed", "corrections": [{"url": "u1"}]}) == []
    assert (
        news_of_correction(old, {"corrections": [{"url": "u1"}, {"url": "u2"}]})[0]["url"] == "u2"
    )
    worse = news_of_correction({"status": "widely_confirmed"}, {"status": "refuted"})
    assert worse and worse[0]["source"] is None


def test_distinctive_names_keep_rare_proper_names() -> None:
    names = distinctive_names(SEED + " По данным Vortexa, танкер шёл из Санкт-Петербурга.", 8)
    assert "minerva" in names and "vortexa" in names
    assert "первая" not in names  # слово в начале предложения — не имя


async def test_ironic_correction_far_by_embedding_is_found_by_name(
    session: AsyncSession,
) -> None:
    seed, sources = await _story(session, with_correction=False)
    # Ирония про капитана и канистры: по эмбеддингу далеко (как 0.53 в жизни), но то же судно.
    await _doc(session, "https://t.me/op/47128", 0.5, 3000, sources[5], JOKE)

    check = await run_check(session, f"doc:{seed.id}", FakeEmbedder(), llm=client(model))
    analysis = json.loads(check.summary)["analysis"]

    # Одна поправка из Telegram без второго источника — «под вопросом», а не «оспорено».
    assert analysis["status"] == "questioned" and analysis["index"] <= CFG.weak_correction_cap
    assert [c["source"] for c in analysis["corrections"]] == ["Оперативная линия"]


async def test_name_match_without_embedding_does_not_break_report(session: AsyncSession) -> None:
    seed, sources = await _story(session, with_correction=False)
    # Свежая публикация: эмбеддинга ещё нет, найдена только по имени; LLM нет — в «пограничные».
    await _doc(session, "https://t.me/op/1", None, 600, sources[5], "Minerva Zen ушла из порта")

    check = await run_check(session, f"doc:{seed.id}", FakeEmbedder())
    report = json.loads(check.summary)

    [found] = [b for b in report["borderline"] if b["url"] == "https://t.me/op/1"]
    assert found["similarity"] is None and found["by_name"] == "minerva"


async def test_correction_in_next_episode_is_still_found(session: AsyncSession) -> None:
    """Как в жизни 14.10.2026: модель сочла пост о прибытии танкера «другим эпизодом»."""
    seed, sources = await _story(session, with_correction=False)
    await _doc(session, "https://t.me/op/47128", 0.5, 3000, sources[5], JOKE)

    def other_episode(request: httpx.Request) -> httpx.Response:
        user = json.loads(request.content)["messages"][1]["content"]
        if "Кандидаты:" in user:
            docs = user.split("Кандидаты:")[1].split("<document id=")[1:]
            return answer(
                {
                    "decisions": [
                        {"id": i, "same_event": False, "reason": "другой эпизод: прибытие"}
                        for i, _ in enumerate(docs, 1)
                    ]
                }
            )
        return model(request)

    check = await run_check(session, f"doc:{seed.id}", FakeEmbedder(), llm=client(other_episode))
    report = json.loads(check.summary)
    analysis = report["analysis"]

    assert report["publications"] == 4  # в историю не вошёл — это другой эпизод
    # Одна поправка из Telegram без второго источника — «под вопросом», а не «оспорено».
    assert analysis["status"] == "questioned" and analysis["index"] <= CFG.weak_correction_cap
    assert [c["source"] for c in analysis["corrections"]] == ["Оперативная линия"]
