"""Language of orch's messages: 中文 (zh), English (en), 日本語 (ja).

Every message in the code is written once, in its source language, and wrapped in tr():

    say(tr("[{0}] 验收通过", task.id))

The source text (with {0}, {1:.2f} ... placeholders) is the key; orch/i18n_catalog.py holds the other
languages. A missing translation falls back to English, then to the source text, so a message is never
lost. tests/test_i18n.py checks that every tr() text has all three languages.

The language is chosen, first match wins:
  1. MAO_LANG environment variable, or `--lang` on the command line
  2. `orch lang set <zh|en|ja>` (saved in %APPDATA%/MAO/settings.json, ~/.config/MAO elsewhere)
  3. the system's display language (Chinese -> zh, Japanese -> ja, anything else -> en)
"""
from __future__ import annotations

import json
import locale
import os
import sys
from pathlib import Path

LANGS = ("zh", "en", "ja")
NAMES = {"zh": "中文", "en": "English", "ja": "日本語"}
_override: str | None = None


def settings_path() -> Path:
    base = os.environ.get("APPDATA") or os.path.join(os.path.expanduser("~"), ".config")
    return Path(os.environ.get("MAO_SETTINGS_FILE") or Path(base) / "MAO" / "settings.json")


def _norm(code: str | None) -> str | None:
    code = (code or "").strip().lower().replace("-", "_")
    if code.startswith("zh"):
        return "zh"
    if code.startswith("ja"):
        return "ja"
    if code.startswith("en"):
        return "en"
    return None


def _system() -> str:
    if sys.platform == "win32":
        try:
            import ctypes

            lcid = ctypes.windll.kernel32.GetUserDefaultUILanguage()
            primary = lcid & 0x3FF
            return {0x04: "zh", 0x11: "ja"}.get(primary, "en")
        except (AttributeError, OSError):
            pass
    for var in ("LC_ALL", "LC_MESSAGES", "LANG"):
        found = _norm(os.environ.get(var))
        if found:
            return found
    try:
        return _norm(locale.getlocale()[0]) or "en"
    except ValueError:
        return "en"


def saved() -> str | None:
    try:
        return _norm(json.loads(settings_path().read_text(encoding="utf-8")).get("lang"))
    except (OSError, ValueError, AttributeError):
        return None


def save(lang: str) -> None:
    lang = _norm(lang) or ""
    if lang not in LANGS:
        raise ValueError(f"language must be one of: {', '.join(LANGS)}")
    path = settings_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    data["lang"] = lang
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")


def current() -> str:
    return _override or _norm(os.environ.get("MAO_LANG")) or saved() or _system()


def set_lang(lang: str | None) -> None:
    """For this process only (the --lang option, tests). None = back to automatic."""
    global _override
    _override = _norm(lang) if lang else None


def tr(text: str, *args, **kwargs) -> str:
    from .i18n_catalog import CATALOG

    lang = current()
    entry = CATALOG.get(text)
    if entry is None or lang == entry.get("src"):
        template = text
    else:
        template = entry.get(lang) or entry.get("en") or text
    if not args and not kwargs:
        return template
    try:
        return template.format(*args, **kwargs)
    except (IndexError, KeyError, ValueError):
        return text.format(*args, **kwargs)
