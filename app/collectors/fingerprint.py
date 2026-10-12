"""Отпечатки текста: точный хэш для дублей и simhash для почти дословных перепечаток (6.3)."""

import hashlib
import re

_WORD = re.compile(r"\w+", re.UNICODE)
_SHINGLE = 3


def _words(text: str) -> list[str]:
    return _WORD.findall(text.lower())


def content_hash(text: str) -> str:
    """SHA-256 текста без учёта регистра, пунктуации и пробелов."""
    return hashlib.sha256(" ".join(_words(text)).encode()).hexdigest()


def simhash(text: str) -> int:
    """64-битный simhash по шинглам из 3 слов, как знаковое int64 (для BIGINT в Postgres)."""
    words = _words(text)
    shingles = [" ".join(words[i : i + _SHINGLE]) for i in range(max(1, len(words) - _SHINGLE + 1))]
    weights = [0] * 64
    for shingle in shingles:
        h = int.from_bytes(hashlib.blake2b(shingle.encode(), digest_size=8).digest(), "big")
        for bit in range(64):
            weights[bit] += 1 if h >> bit & 1 else -1
    value = sum(1 << bit for bit in range(64) if weights[bit] > 0)
    return value - (1 << 64) if value >= 1 << 63 else value


def hamming(a: int, b: int) -> int:
    return ((a ^ b) & ((1 << 64) - 1)).bit_count()
