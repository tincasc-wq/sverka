"""Проверка скриншотов: распознавание текста Tesseract и доступ по тарифу."""

import io

import pytest

from app import ocr
from app.models import User
from app.web.session import COOKIE, sign
from tests.test_site import client, db  # noqa: F401 — фикстура

FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
NEWS = "В Лиможе протестующие подожгли историческое здание мэрии"


def _png(text: str) -> bytes:
    from PIL import Image, ImageDraw, ImageFont

    image = Image.new("RGB", (1400, 160), "white")
    ImageDraw.Draw(image).text((20, 50), text, fill="black", font=ImageFont.truetype(FONT, 40))
    out = io.BytesIO()
    image.save(out, format="PNG")
    return out.getvalue()


def test_clean_joins_hyphens_and_drops_noise() -> None:
    raw = "Протестую-\nщие подожгли мэрию\n|||  ©  ~~\n\n\nПожар потушили к утру"
    assert ocr.clean(raw) == "Протестующие подожгли мэрию\n\nПожар потушили к утру"


def test_rejects_non_images_and_big_files() -> None:
    with pytest.raises(ocr.OcrError, match="не картинка"):
        ocr.recognize(b"%PDF-1.7 ...")
    with pytest.raises(ocr.OcrError, match="8 МБ"):
        ocr.recognize(b"\x89PNG\r\n\x1a\n" + b"0" * (ocr.MAX_BYTES + 1))
    assert ocr.is_image(b"RIFF\x00\x00\x00\x00WEBPVP8 ") and not ocr.is_image(b"RIFF....WAVE")


@pytest.mark.skipif(not ocr.available(), reason="Tesseract не установлен")
def test_tesseract_reads_russian_screenshot() -> None:
    text = ocr.recognize(_png(NEWS)).lower()
    assert "лиможе" in text and "мэрии" in text


async def test_site_screenshot_check_for_paid_plans_only(db, monkeypatch) -> None:  # noqa: F811
    from app.web import site

    jobs: list[tuple] = []

    async def fake_enqueue(job: str, *args, **options) -> None:
        jobs.append((job, *args))

    async def fake_recognize(data: bytes) -> str:
        return NEWS + ". Колокольня обрушилась вечером, пострадавших нет."

    monkeypatch.setattr(site, "enqueue", fake_enqueue)
    monkeypatch.setattr(ocr, "recognize_async", fake_recognize)
    async with db() as s:
        pro, free = User(email="ocr-pro@site.test", plan="pro"), User(email="ocr-free@site.test")
        s.add_all([pro, free])
        await s.commit()
    upload = {
        "image": ("shot.png", _png("x") if ocr.available() else b"\x89PNG\r\n\x1a\n", "image/png")
    }
    async with client() as c:
        c.cookies.set(COOKIE, sign(free.id))
        home_free = await c.get("/")
        refused = await c.post("/check-image", files=upload)
    async with client() as c:
        c.cookies.set(COOKIE, sign(pro.id))
        home_pro = await c.get("/")
        done = await c.post("/check-image", files=upload)

    assert 'name="image"' not in home_free.text and "Скриншот новости?" in home_free.text
    assert refused.status_code == 422 and "Проверка скриншотов доступна" in refused.text
    assert 'name="image"' in home_pro.text
    assert done.status_code == 303 and done.headers["location"].startswith("/c/")
    assert jobs and jobs[0][0] == "web_check"


def test_telegram_post_image_urls_and_mark() -> None:
    from app.post_images import MARK, telegram_image_urls, with_image_text

    html = (
        '<a class="tgme_widget_message_photo_wrap 1" style="width:800px;'
        "background-image:url('https://cdn4.telesco.pe/file/a.jpg')\"></a>"
        '<a class="tgme_widget_message_photo_wrap 2" style="background-image:url('
        "'https://cdn4.telesco.pe/file/b.jpg')\"></a>"
        '<i class="tgme_widget_message_user_photo" style="background-image:url(\'https://x/ava.jpg\')">'
    )
    assert telegram_image_urls(html) == [
        "https://cdn4.telesco.pe/file/a.jpg",
        "https://cdn4.telesco.pe/file/b.jpg",
    ]  # аватар канала — не картинка поста
    text = with_image_text("Вот документ:", ["Указ Президента Российской Федерации"])
    assert text == f"Вот документ:\n\n{MARK}\n\nУказ Президента Российской Федерации"
    assert with_image_text("Пост", []) == "Пост"


async def test_ocr_images_checkbox_is_remembered_for_paid_only(db, monkeypatch) -> None:  # noqa: F811
    from app.web import site

    async def fake_enqueue(job: str, *args, **options) -> None:
        return None

    monkeypatch.setattr(site, "enqueue", fake_enqueue)
    async with db() as s:
        pro, free = User(email="img-pro@site.test", plan="pro"), User(email="img-free@site.test")
        s.add_all([pro, free])
        await s.commit()
    link = {"value": "https://t.me/rian_ru/1", "ocr_images": "on"}
    async with client() as c:
        c.cookies.set(COOKIE, sign(pro.id))
        await c.post("/check", data=link)
        home = await c.get("/")
    async with client() as c:
        c.cookies.set(COOKIE, sign(free.id))
        await c.post("/check", data=link)
        free_home = await c.get("/")
    async with db() as s:
        assert (await s.get(User, pro.id)).ocr_images is True
        assert (await s.get(User, free.id)).ocr_images is False
    assert 'name="ocr_images" checked' in home.text
    assert 'name="ocr_images"' not in free_home.text
