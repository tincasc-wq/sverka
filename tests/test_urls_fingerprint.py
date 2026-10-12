import pytest

from app.collectors.fingerprint import content_hash, hamming, simhash
from app.collectors.urls import normalize_url


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("http://www.Example.ru/news/1/", "https://example.ru/news/1"),
        ("https://example.ru/news/1?utm_source=tg&utm_medium=x", "https://example.ru/news/1"),
        ("https://example.ru/news/1?b=2&a=1&fbclid=zzz", "https://example.ru/news/1?a=1&b=2"),
        ("https://example.ru/news/1/amp/", "https://example.ru/news/1"),
        ("https://example.ru/amp/news/1", "https://example.ru/news/1"),
        ("https://example.ru/news/1#comments", "https://example.ru/news/1"),
        ("https://example.ru", "https://example.ru/"),
        ("https://example.ru:8443/a", "https://example.ru:8443/a"),
        ("https://lenta.ru/news/1/?rcmclid=49b06cb8", "https://lenta.ru/news/1"),
    ],
)
def test_normalize_url(raw: str, expected: str) -> None:
    assert normalize_url(raw) == expected


def test_content_hash_ignores_case_punctuation_and_spaces() -> None:
    assert content_hash("Пожар  в Москве!") == content_hash("пожар в москве")
    assert content_hash("Пожар в Москве") != content_hash("Пожар в Казани")


ARTICLE = (
    "В Подольске утром в понедельник загорелся складской комплекс площадью две тысячи "
    "квадратных метров. По данным ГУ МЧС по Московской области, на месте работают сорок "
    "пожарных и двенадцать единиц техники. Возгорание удалось локализовать спустя три часа. "
    "Пострадавших нет, из соседних зданий эвакуировали около ста человек. Причины пожара "
    "устанавливаются, на месте работают дознаватели. Прокуратура начала проверку соблюдения "
    "правил пожарной безопасности, владелец склада пока не дал комментариев. Движение по "
    "соседней улице было перекрыто, сейчас оно восстановлено, сообщили в администрации округа."
)
OTHER = (
    "Банк России сохранил ключевую ставку на прежнем уровне, следует из сообщения регулятора. "
    "Аналитики ожидали такого решения и прогнозируют замедление инфляции к концу года. Рубль "
    "после публикации решения укрепился, индекс Мосбиржи вырос на полтора процента. Следующее "
    "заседание совета директоров по ставке запланировано на декабрь, сообщили в пресс-службе."
)


def test_simhash_close_for_near_copies_and_far_for_different_texts() -> None:
    # Перепечатка со своим хвостом. Порог для графа (6.3) калибруется на этапе 3:
    # на коротких текстах каждая добавленная фраза сдвигает отпечаток заметно сильнее.
    reprint = "Срочно. " + ARTICLE + " Подробности уточняются."

    assert hamming(simhash(ARTICLE), simhash(reprint)) <= 8
    assert hamming(simhash(ARTICLE), simhash(OTHER)) > 20


def test_simhash_fits_signed_bigint() -> None:
    value = simhash("любой текст для проверки диапазона")
    assert -(2**63) <= value < 2**63
