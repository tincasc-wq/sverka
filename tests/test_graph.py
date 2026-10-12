from datetime import UTC, datetime, timedelta

import pytest

from app.collectors.fingerprint import simhash
from app.core.scoring import GraphConfig
from app.models.enums import DerivationKind
from app.pipeline.graph import (
    Node,
    SourceInfo,
    attributed_sources,
    build_graph,
    cites_official_document,
)

CFG = GraphConfig(near_copy_max_hamming=8)
T0 = datetime(2026, 10, 12, 9, 0, tzinfo=UTC)

TASS = SourceInfo(1, "ТАСС", "tass.ru", "agency", owner="ТАСС", aliases=("ТАСС",))
TASS_TG = SourceInfo(2, "ТАСС (TG)", "@tass_agency", "tg", owner="ТАСС")
RBC = SourceInfo(3, "РБК", "rbc.ru", "media", owner="РБК", aliases=("РБК",))
MASH = SourceInfo(4, "Mash", "@breakingmash", "tg", aliases=("Mash",))
KOMMERSANT = SourceInfo(5, "Коммерсантъ", "kommersant.ru", "media", aliases=("Коммерсантъ", "Ъ"))
MK = SourceInfo(6, "МК", "mk.ru", "media", aliases=("МК",))
KREMLIN = SourceInfo(7, "Президент России", "kremlin.ru", "official", aliases=("Кремля",))
SOURCES = [TASS, TASS_TG, RBC, MASH, KOMMERSANT, MK, KREMLIN]

ARTICLE = (
    "В Подольске утром загорелся складской комплекс площадью две тысячи квадратных метров. "
    "На месте работают сорок пожарных и двенадцать единиц техники. Возгорание удалось "
    "локализовать спустя три часа. Пострадавших нет, из соседних зданий эвакуировали около "
    "ста человек. Причины пожара устанавливаются, на месте работают дознаватели."
)


def node(id, source, minutes, text="", links=(), url=None, claimed_minutes=None):
    observed = T0 + timedelta(minutes=minutes)
    claimed = T0 + timedelta(minutes=claimed_minutes) if claimed_minutes is not None else observed
    return Node(
        id=id,
        url=url or f"https://{source.handle_or_domain.lstrip('@')}/news/{id}",
        observed_at=observed,
        claimed_at=claimed,
        source=source,
        text=text,
        links=tuple(links),
        simhash=simhash(text) if text else None,
    )


def test_reprint_points_to_earliest_original() -> None:
    original = node(1, TASS, 0, ARTICLE)
    reprint = node(2, RBC, 30, "Срочно. " + ARTICLE + " Подробности уточняются.")

    graph = build_graph([reprint, original], SOURCES, CFG)

    assert graph.primary_id == 1
    assert [(e.from_id, e.to_id, e.kind) for e in graph.edges] == [(1, 2, DerivationKind.NEAR_COPY)]
    assert graph.independent_groups == ["ТАСС"]


def test_attribution_links_to_source_in_story() -> None:
    tass = node(1, TASS, 0, "Склад загорелся в Подольске.")
    mash = node(2, MASH, 20, "Как сообщает ТАСС, в Подольске горит склад, работают пожарные.")

    graph = build_graph([tass, mash], SOURCES, CFG)

    assert [(e.from_id, e.to_id, e.kind) for e in graph.edges] == [
        (1, 2, DerivationKind.ATTRIBUTION)
    ]


def test_attribution_to_source_outside_story_is_reported() -> None:
    mash = node(1, MASH, 0, "В Подольске горит склад, сообщает РБК со ссылкой на источники.")

    graph = build_graph([mash], SOURCES, CFG)

    assert graph.edges == []
    assert graph.primary_id == 1
    assert graph.external_attributions == {1: ["РБК"]}


def test_same_owner_group_is_one_report_not_a_borrowing() -> None:
    site = node(1, TASS, 0, ARTICLE)
    channel = node(2, TASS_TG, 5, ARTICLE)  # тот же текст в канале той же редакции
    rbc = node(3, RBC, 40, "Пожар на складе в Подольске потушили, пострадавших нет.")

    graph = build_graph([site, channel, rbc], SOURCES, CFG)

    assert graph.edges == []
    assert graph.independent_groups == ["ТАСС", "РБК"]


def test_explicit_link_to_post_and_to_channel() -> None:
    post = node(1, MASH, 0, "Горит склад.", url="https://t.me/breakingmash/101")
    by_post = node(2, RBC, 10, "Пожар.", links=["https://t.me/breakingmash/101"])
    by_channel = node(3, KOMMERSANT, 20, "Пожар!", links=["https://t.me/breakingmash"])

    graph = build_graph([post, by_post, by_channel], SOURCES, CFG)

    kinds = {(e.from_id, e.to_id): e.kind for e in graph.edges}
    assert kinds == {(1, 2): DerivationKind.EXPLICIT_LINK, (1, 3): DerivationKind.EXPLICIT_LINK}


def test_future_claimed_date_is_not_trusted() -> None:
    # Источник заявляет время на 3 часа позже, чем мы увидели публикацию: верим наблюдению.
    late_claim = node(1, RBC, 0, "Текст", claimed_minutes=180)
    honest = node(2, TASS, 30, "Текст другой")

    assert late_claim.effective_at(CFG) == T0
    assert late_claim.date_unreliable(CFG)
    assert not honest.date_unreliable(CFG)


def test_backdated_claim_moves_publication_earlier_in_timeline() -> None:
    # Сайт опубликовал раньше, чем мы его опросили: в хронологии — заявленное время.
    early = node(1, KOMMERSANT, 60, ARTICLE, claimed_minutes=-30)
    tass = node(2, TASS, 0, "Срочно. " + ARTICLE)

    graph = build_graph([tass, early], SOURCES, CFG)

    assert graph.primary_id == 1


def test_short_aliases_match_whole_words_case_sensitive() -> None:
    text_lower = "По данным мк-района, объект закрыт, сообщили жители."
    text_real = "Как пишет «Ъ», склад закрыт. По данным МК, жертв нет."

    assert attributed_sources(text_lower, SOURCES, 60) == set()
    assert attributed_sources(text_real, SOURCES, 60) == {KOMMERSANT.id, MK.id}


def test_alias_without_attribution_phrase_is_ignored() -> None:
    text = "Журналист ТАСС приехал на место пожара и снял видео."

    assert attributed_sources(text, SOURCES, 60) == set()


def test_retelling_official_decree_derives_from_official_publication() -> None:
    decree = node(1, KREMLIN, 0, "Владимир Путин подписал Указ о безвизовом въезде.")
    rbc = node(2, RBC, 50, "Граждане смогут въезжать без визы, следует из опубликованного указа.")
    mash = node(3, MASH, 60, "Безвиз ввели, документ размещён на портале правовой информации.")
    unrelated = node(4, KOMMERSANT, 70, "Туроператоры ждут роста турпотока, указал глава АТОР.")

    graph = build_graph([decree, rbc, mash, unrelated], SOURCES, CFG)

    kinds = {(e.from_id, e.to_id): (e.kind, e.confidence) for e in graph.edges}
    assert kinds == {
        (1, 2): (DerivationKind.ATTRIBUTION, 0.6),
        (1, 3): (DerivationKind.ATTRIBUTION, 0.6),
    }
    assert graph.primary_id == 1
    assert graph.independent_groups == ["kremlin.ru", "kommersant.ru"]


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Об этом говорится в президентском указе.", True),
        ("Соответствующий документ подписал Владимир Путин.", True),
        ("Документ размещён на портале правовой информации.", True),
        ("Как следует из документа, режим продлится год.", True),
        ("Эксперт указал на риски для туристов.", False),
        ("Путин ввёл безвиз для граждан Вьетнама.", False),
    ],
)
def test_official_document_phrases(text: str, expected: bool) -> None:
    assert cites_official_document(text) is expected


def test_backfilled_publication_is_not_flagged_as_unreliable() -> None:
    tracked = SourceInfo(9, "Новый источник", "new.ru", "media", tracked_since=T0)
    backfill = Node(
        id=1,
        url="https://new.ru/1",
        observed_at=T0 + timedelta(minutes=5),
        claimed_at=T0 - timedelta(days=1),
        source=tracked,
    )
    suspicious = Node(
        id=2,
        url="https://new.ru/2",
        observed_at=T0 + timedelta(days=2),
        claimed_at=T0 + timedelta(days=1),
        source=tracked,
    )

    assert not backfill.date_unreliable(CFG)
    assert suspicious.date_unreliable(CFG)


def test_attribution_to_source_outside_registry_is_named() -> None:
    post = node(
        1,
        MASH,
        0,
        "В организации покушения подозревают «правых экстремистов», сообщает Wyborcza "
        "со ссылкой на неназванного чиновника. По данным МВД Польши, задержан один человек.",
    )

    graph = build_graph([post], SOURCES, CFG)

    assert graph.external_attributions == {1: ["Wyborcza", "МВД Польши"]}


def test_known_source_is_not_duplicated_as_unknown_name() -> None:
    post = node(1, MASH, 0, "Как сообщает ТАСС, в Подольске горит склад.")

    graph = build_graph([post], SOURCES, CFG)

    assert graph.external_attributions == {1: ["ТАСС"]}


def test_unknown_names_stop_at_line_break_and_skip_known_domains() -> None:
    post = node(1, MASH, 0, "Об этом сообщает KP.RU.\nПо словам очевидцев, жильцов эвакуировали.")

    graph = build_graph([post], [*SOURCES, SourceInfo(8, "КП", "kp.ru", "media")], CFG)

    assert graph.external_attributions == {}


def test_four_word_names_and_unknown_channels_are_named() -> None:
    post = Node(
        id=1,
        url="https://t.me/Warhronika/16555",
        observed_at=T0,
        text="План представлен генералом, сообщает The Wall Street Journal со ссылкой на штаб.",
    )

    graph = build_graph([post], SOURCES, CFG)

    assert post.group == "@Warhronika"
    assert graph.external_attributions == {1: ["The Wall Street Journal"]}


def test_english_attribution_phrases() -> None:
    nyt = SourceInfo(8, "The New York Times", "nytimes.com", "media", aliases=("New York Times",))
    text = "Two officials were detained, according to The New York Times, citing police sources."
    ru = "Как пишет The New York Times, задержаны двое чиновников."

    assert attributed_sources(text, [*SOURCES, nyt], 60) == {nyt.id}
    assert attributed_sources(ru, [*SOURCES, nyt], 60) == {nyt.id}
    post = node(1, MASH, 0, "The strike killed 12 people, according to Reuters.")
    assert build_graph([post], SOURCES, CFG).external_attributions == {1: ["Reuters"]}


def test_blocked_source_is_labelled_in_report() -> None:
    from app.pipeline.report import build_report  # noqa: F401 — имя берётся из display_name

    bbc = SourceInfo(9, "BBC News", "bbc.com", "media", legal_note="заблокировано в РФ")
    assert bbc.display_name == "BBC News (заблокировано в РФ)"
    assert TASS.display_name == "ТАСС"
