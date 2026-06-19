"""Kernel i18n — language-aware string tables.

Usage: t(lang, key, **kwargs)
  lang: language code ("en", "zh", "wen")
  key: dotted string ID ("system.current_time")
  kwargs: template substitutions

The kernel ships en.json (English), zh.json (中文), and wen.json (文言).
Additional languages can be registered by the lingtai package via
register_strings(). Unknown language falls back to English. Unknown
key returns the key itself.
"""
from __future__ import annotations

import json
from pathlib import Path

_DIR = Path(__file__).parent
_CACHE: dict[str, dict[str, str]] = {}


def _load(lang: str) -> dict[str, str]:
    """Load and cache a language file. Returns empty dict if not found."""
    if lang not in _CACHE:
        path = _DIR / f"{lang}.json"
        if path.is_file():
            _CACHE[lang] = json.loads(path.read_text(encoding="utf-8"))
        else:
            _CACHE[lang] = {}
    return _CACHE[lang]


def register_strings(lang: str, strings: dict[str, str]) -> None:
    """Register (or extend) a language's string table.

    Called by lingtai to inject non-English translations into the
    kernel's cache so that kernel-level t() calls resolve correctly.
    Merges into any existing entries for the language.
    """
    table = _CACHE.setdefault(lang, {})
    table.update(strings)


def t(lang: str, key: str, **kwargs) -> str:
    """Translate a key. Falls back to English, then returns the key itself.

    Extra kwargs not referenced in the template are silently ignored
    (needed because en/zh templates may use different subsets of kwargs).
    """
    from collections import defaultdict

    table = _load(lang)
    value = table.get(key)
    if value is None and lang != "en":
        value = _load("en").get(key)
    if value is None:
        return key
    if kwargs:
        return value.format_map(defaultdict(str, kwargs))
    return value
