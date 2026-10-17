"""Длинный пост со справкой: заголовок отдельно, широкая полоса для LLM, свой источник издания."""

from datetime import timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Document
from app.pipeline.graph import Node, SourceInfo
from app.pipeline.sourcing import cites_own_source, own_names
from app.pipeline.story import build_story, story_headline
from tests.test_story import T0, _doc


async def test_moderately_close_near_in_time_goes_to_llm_and_copies_judged_once(
    session: AsyncSession,
) -> None:
    seed = await _doc(session, "https://t.me/x/1", 1.0, 0, text="Протестующие подожгли ратушу")
    near = await _doc(session, "https://ria.ru/1", 0.65, 40, title="Протестующие подожгли мэрию")
    far = await _doc(session, "https://ria.ru/2", 0.65, 60 * 20, title="Мэрию подожгли снова")
    copy_title = "Радикалы подожгли историческое здание во Франции — видео"
    copies = [
        await _doc(session, f"https://{city}.ru/1", 0.64, 90, title=copy_title)
        for city in ("ngs", "e1", "74")
    ]
    weak = await _doc(session, "https://ria.ru/3", 0.55, 30, title="Погода в Лиможе на выходные")

    seen: list[int] = []

    async def judge(seed_doc: Document, docs: list[Document]) -> dict[int, tuple[bool, str]]:
        seen.extend(d.id for d in docs)
        return {d.id: (True, "то же событие") for d in docs}

    result = await build_story(session, seed, judge=judge, persist=False)
    members = {c.document.id for c in result.members}

    assert near.id in members and all(c.id in members for c in copies)
    assert far.id not in members and weak.id not in members  # далеко по времени / слишком далеко
    assert len([i for i in seen if i in {c.id for c in copies}]) == 1  # копии — модели один раз
    assert far.id not in seen and weak.id not in seen


async def test_without_llm_wide_band_does_nothing(session: AsyncSession) -> None:
    seed = await _doc(session, "https://t.me/x/2", 1.0, 0, text="Протестующие подожгли ратушу")
    near = await _doc(session, "https://ria.ru/4", 0.65, 40, title="Протестующие подожгли мэрию")
    result = await build_story(session, seed, persist=False)
    assert near.id not in {c.document.id for c in result.members}


def test_story_headline() -> None:
    post = Document(
        url="https://t.me/x/3",
        text="Протестующие во Франции подожгли историческую ратушу в городе Лимож\n"
        "Ратуша в Лиможе была построена в период с 1876 по 1883 год и официально открыта "
        "14 июля 1883 года. Ратуша является историческим памятником.",
    )
    short = Document(
        url="https://t.me/x/4", text="Протестующие во Франции подожгли ратушу в Лиможе"
    )
    article = Document(url="https://a.ru/1", title="Сгорела ратуша в Лиможе", text="Длинный " * 40)
    assert (
        story_headline(post)
        == "Протестующие во Франции подожгли историческую ратушу в городе Лимож"
    )
    assert story_headline(short) is None  # текст и есть заголовок
    assert story_headline(article) == "Сгорела ратуша в Лиможе"
    assert story_headline(Document(url="u", text="Взрыв\n" + "подробности " * 30)) is None


def test_own_source_phrase() -> None:
    ria = SourceInfo(1, "РИА Новости", "ria.ru", "agency", owner="МИА", aliases=("РИА Новости",))
    ria_tg = SourceInfo(2, "РИА Новости (TG)", "@rian_ru", "tg", owner="МИА")
    kommersant = SourceInfo(3, "Коммерсантъ", "kommersant.ru", "media", aliases=("Коммерсантъ",))
    tass = SourceInfo(4, "ТАСС", "tass.ru", "agency", owner="ТАСС", aliases=("ТАСС",))
    sources = {s.id: s for s in (ria, ria_tg, kommersant, tass)}

    def node(source: SourceInfo, text: str) -> Node:
        return Node(
            id=source.id,
            url=f"https://{source.handle_or_domain}/1",
            observed_at=T0,
            claimed_at=T0 + timedelta(0),
            source=source,
            text=text,
        )

    ria_text = "Проверка проходит на Сенном рынке, сообщили РИА Новости в силовых структурах."
    kommersant_text = "Об этом сообщили «РИА Новости» в силовых структурах. Рынок закрыли."
    tg_text = "Источник РИА Новости в правоохранительных органах подтвердил задержания."

    for n, expected in [
        (node(ria, ria_text), True),
        (node(kommersant, kommersant_text), False),  # чужой источник — пересказ
        (node(ria_tg, tg_text), True),  # канал той же редакции
        (node(tass, "Как пишет ТАСС со ссылкой на РИА, рынок закрыт."), False),
        (node(tass, "Об этом рассказал ТАСС собеседник в оперативных службах."), True),
    ]:
        assert cites_own_source(n.text, own_names(n, sources)) is expected, n.text
