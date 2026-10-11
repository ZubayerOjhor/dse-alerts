"""Two languages, one catalog each: ``en.json`` and ``bn.json`` in this package.

* ``t(lang, key, **kw)`` looks the key up in the chosen language, falls back to English,
  then to the key itself, and formats ``{placeholders}`` with ``kw``.
* The web UI resolves the language from a cookie, then the user's saved preference, then
  ``APP_DEFAULT_LANG``. Email and Telegram use the user's saved preference.
* Numbers stay in Latin digits in both languages (what Bangladeshi brokerages print);
  the currency sign is ৳ in the UI and "Tk" in plain text.

Adding a language: drop ``xx.json`` next to these, add it to ``LANGS``.
"""

from __future__ import annotations

import json
from functools import cache
from pathlib import Path
from typing import Any

HERE = Path(__file__).parent
LANGS: dict[str, str] = {"en": "English", "bn": "বাংলা"}
DEFAULT = "en"


@cache
def catalog(lang: str) -> dict[str, str]:
    p = HERE / f"{lang}.json"
    if not p.exists():
        return {}
    return json.loads(p.read_text(encoding="utf-8"))


def normalize(lang: str | None) -> str:
    lang = (lang or "").strip().lower()
    return lang if lang in LANGS else DEFAULT


def t(lang: str | None, key: str, **kw: Any) -> str:
    """Translate ``key`` into ``lang`` (English fallback) and fill in ``kw``."""
    lang = normalize(lang)
    s = catalog(lang).get(key)
    if s is None:
        s = catalog(DEFAULT).get(key, key)
    if kw:
        try:
            return s.format(**kw)
        except (KeyError, IndexError, ValueError):
            return s
    return s


def plural(lang: str | None, n: int, one_key: str, many_key: str, **kw: Any) -> str:
    """Pick the singular or plural key for ``n`` (both languages take the same rule)."""
    return t(lang, one_key if n == 1 else many_key, n=n, **kw)


def missing_keys(lang: str) -> list[str]:
    """English keys without a translation in ``lang``; used by the tests."""
    return sorted(set(catalog(DEFAULT)) - set(catalog(lang)))
