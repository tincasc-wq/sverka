"""Распознавание текста с картинки (Tesseract на нашем сервере) — для платных тарифов.

Скриншот «новости» — частый формат фейков, а пост-картинку без подписи проверить было нечем.
Распознанный текст проверяется как обычный текст новости. Tesseract не нужен сети и ключей;
работает на процессоре, поэтому одновременно — не больше двух распознаваний.
"""

import asyncio
import os
import re
import shutil
import subprocess

# Кириллица и латиница: украинский и белорусский не замедляют (замер 18.10.2026: 0.25 с с ними и
# без), но без них «України повідомило» читается как «Укра!ни пов!домило».
LANGS = "rus+eng+ukr+bel"
# «Нестандартные языки»: сначала определяем письменность (OSD, 0.1–0.3 с), потом — нужная модель.
SCRIPT_LANGS = {"Han": "chi_sim+chi_tra+eng", "Japanese": "jpn+eng"}
MAX_BYTES = 8 * 1024 * 1024
TIMEOUT_SECONDS = 60
MIN_CHARS = 40  # меньше — это не новость, а подпись к фото или мусор распознавания
MIN_CJK_CHARS = 15  # иероглифы плотнее: 15 знаков — уже фраза
_CJK = re.compile(r"[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uac00-\ud7af]")
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


_LETTERS = re.compile(r"[^\W\d_]")  # буквы любого письма: кириллица, латиница, иероглифы


def enough(text: str) -> bool:
    """Достаточно ли текста, чтобы это была новость, а не подпись или мусор."""
    return len(text) >= MIN_CHARS or len(_CJK.findall(text)) >= MIN_CJK_CHARS


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


def _tesseract(data: bytes, args: list[str]) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(
            ["tesseract", "stdin", "stdout", *args],
            input=data,
            capture_output=True,
            timeout=TIMEOUT_SECONDS,
            check=False,
            # Не больше двух ядер: на том же сервере считаются эмбеддинги (4 ядра на всё).
            env={**os.environ, "OMP_THREAD_LIMIT": "2"},
        )
    except subprocess.TimeoutExpired as exc:
        raise OcrError("распознавание заняло слишком много времени") from exc


def detect_script(data: bytes) -> str | None:
    """Письменность на картинке (Cyrillic, Latin, Han, Japanese…); None — не определилась."""
    done = _tesseract(data, ["--psm", "0", "-l", "osd"])
    out = done.stdout.decode("utf-8", errors="replace") + done.stderr.decode(
        "utf-8", errors="replace"
    )
    match = re.search(r"Script:\s*(\w+)", out)
    return match.group(1) if match else None


def recognize(data: bytes, rare_langs: bool = False) -> str:
    """rare_langs — «нестандартные языки»: при иероглифах распознаём китайским или японским."""
    if len(data) > MAX_BYTES:
        raise OcrError("картинка больше 8 МБ")
    if not is_image(data):
        raise OcrError("это не картинка (поддерживаются JPEG, PNG, WebP, GIF, BMP)")
    if not available():
        raise OcrError("распознавание текста на сервере не установлено")
    script = detect_script(data) if rare_langs else None
    text = _read(data, SCRIPT_LANGS.get(script or "", LANGS))
    if rare_langs and script is None and not enough(text):
        # Письменность не определилась (одна короткая строка), а кириллицей вышел мусор —
        # пробуем иероглифы. Русский текст сюда не доходит: он читается с первого раза.
        cjk = _read(data, ALL_CJK)
        if len(_CJK.findall(cjk)) > len(_LETTERS.findall(text)) // 2:
            return cjk
    return text


ALL_CJK = "chi_sim+jpn+eng"


def _read(data: bytes, langs: str) -> str:
    done = _tesseract(data, ["-l", langs, "--psm", "3"])
    if done.returncode != 0:
        raise OcrError("не удалось прочитать картинку")
    return clean(done.stdout.decode("utf-8", errors="replace"))


async def recognize_async(data: bytes, rare_langs: bool = False) -> str:
    async with _slots:
        return await asyncio.to_thread(recognize, data, rare_langs)
