"""Распознавание текста с картинки (Tesseract на нашем сервере) — для платных тарифов.

Скриншот «новости» — частый формат фейков, а пост-картинку без подписи проверить было нечем.
Распознанный текст проверяется как обычный текст новости. Tesseract не нужен сети и ключей;
работает на процессоре, поэтому одновременно — не больше двух распознаваний.
"""

import asyncio
import re
import shutil
import subprocess

LANGS = "rus+eng"
MAX_BYTES = 8 * 1024 * 1024
TIMEOUT_SECONDS = 60
MIN_CHARS = 40  # меньше — это не новость, а подпись к фото или мусор распознавания
_slots = asyncio.Semaphore(2)

# Сигнатуры форматов: не отдаём Tesseract что попало.
_SIGNATURES = (b"\x89PNG\r\n\x1a\n", b"\xff\xd8\xff", b"RIFF", b"GIF8", b"BM")


class OcrError(Exception):
    """Не удалось распознать: не картинка, слишком большая, Tesseract не установлен и т. п."""


def is_image(data: bytes) -> bool:
    if data[:4] == b"RIFF":
        return data[8:12] == b"WEBP"
    return any(data.startswith(s) for s in _SIGNATURES)


def available() -> bool:
    return shutil.which("tesseract") is not None


_LETTERS = re.compile(r"[A-Za-zА-Яа-яЁё]")


def clean(raw: str) -> str:
    """Склеить переносы, убрать строки-мусор (рамки, иконки), лишние пробелы."""
    text = re.sub(r"(\w)-\n(\w)", r"\1\2", raw)  # перенос слова по слогам
    lines = []
    for line in text.splitlines():
        line = " ".join(line.split())
        if not line:
            if lines and lines[-1]:
                lines.append("")
            continue
        letters = len(_LETTERS.findall(line))
        if letters >= 3 and letters / len(line) >= 0.4:
            lines.append(line)
    return "\n".join(lines).strip()


def recognize(data: bytes) -> str:
    if len(data) > MAX_BYTES:
        raise OcrError("картинка больше 8 МБ")
    if not is_image(data):
        raise OcrError("это не картинка (поддерживаются JPEG, PNG, WebP, GIF, BMP)")
    if not available():
        raise OcrError("распознавание текста на сервере не установлено")
    try:
        done = subprocess.run(
            ["tesseract", "stdin", "stdout", "-l", LANGS, "--psm", "3"],
            input=data,
            capture_output=True,
            timeout=TIMEOUT_SECONDS,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise OcrError("распознавание заняло слишком много времени") from exc
    if done.returncode != 0:
        raise OcrError("не удалось прочитать картинку")
    return clean(done.stdout.decode("utf-8", errors="replace"))


async def recognize_async(data: bytes) -> str:
    async with _slots:
        return await asyncio.to_thread(recognize, data)
