"""Промпты LLM лежат в prompts/<шаг>.md (ТЗ 10.3: промпты — код).

Первая строка файла — `version: <метка>`; она пишется в llm_calls.prompt_version, чтобы
после смены промпта было видно, какие проверки сделаны какой версией.
"""

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

PROMPTS_DIR = Path(__file__).resolve().parents[2] / "prompts"


@dataclass(frozen=True)
class Prompt:
    name: str
    version: str
    text: str


@lru_cache
def load_prompt(name: str) -> Prompt:
    raw = (PROMPTS_DIR / f"{name}.md").read_text(encoding="utf-8")
    first, _, rest = raw.partition("\n")
    if not first.startswith("version:"):
        raise ValueError(f"prompts/{name}.md: first line must be 'version: ...'")
    return Prompt(name=name, version=f"{name}@{first.split(':', 1)[1].strip()}", text=rest.strip())
