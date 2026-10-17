"""Language codes used by Immersive Translate and friends, normalised to one canonical form."""

from __future__ import annotations

import re

LANG_NAMES: dict[str, str] = {
    "zh-CN": "Simplified Chinese",
    "zh-TW": "Traditional Chinese",
    "en": "English",
    "ja": "Japanese",
    "ko": "Korean",
    "fr": "French",
    "de": "German",
    "es": "Spanish",
    "pt": "Portuguese",
    "it": "Italian",
    "ru": "Russian",
    "vi": "Vietnamese",
    "th": "Thai",
    "id": "Indonesian",
    "ms": "Malay",
    "ar": "Arabic",
    "hi": "Hindi",
    "tr": "Turkish",
    "nl": "Dutch",
    "pl": "Polish",
    "uk": "Ukrainian",
}

_ZH_CN = {"zh", "zh-cn", "zh-hans", "zh-sg", "zh-hans-cn", "chs"}
_ZH_TW = {"zh-tw", "zh-hant", "zh-hk", "zh-mo", "zh-hant-tw", "cht"}


def canon(code: str | None, default: str = "zh-CN") -> str:
    """Normalise a language code: 'zh-hans' -> 'zh-CN', 'en-US' -> 'en', '*' stays '*'."""
    if not code:
        return default
    c = code.strip().replace("_", "-").lower()
    if c in ("*", "any"):
        return "*"
    if c == "auto":
        return "auto"
    if c in _ZH_CN:
        return "zh-CN"
    if c in _ZH_TW:
        return "zh-TW"
    base = c.split("-")[0]
    return base or default


def lang_name(code: str) -> str:
    c = canon(code)
    return LANG_NAMES.get(c, code)


_NAME_PATTERNS = [
    (re.compile(r"\b(?:to|into)\s+(?:the\s+)?" + re.escape(name) + r"\b", re.I), code)
    for code, name in sorted(LANG_NAMES.items(), key=lambda kv: -len(kv[1]))
]
_ALIASES = [
    (re.compile(r"\b(?:to|into)\s+(?:Chinese|Mandarin)\b", re.I), "zh-CN"),
    (re.compile(r"简体中文|简体"), "zh-CN"),
    (re.compile(r"繁體中文|繁体中文|繁體"), "zh-TW"),
]


def detect_target_from_prompt(text: str) -> str | None:
    """Best-effort: find 'translate ... to <Language>' in a prompt written by a client."""
    for pattern, code in _NAME_PATTERNS:
        if pattern.search(text):
            return code
    for pattern, code in _ALIASES:
        if pattern.search(text):
            return code
    return None


_CJK = re.compile(r"[぀-ヿ㐀-䶿一-鿿가-힯]")


def guess_source_lang(text: str) -> str:
    """Very rough detection, only used to fill `detected_source_lang`."""
    if not text:
        return "en"
    cjk = len(_CJK.findall(text))
    if cjk and cjk / max(len(text), 1) > 0.2:
        if re.search(r"[぀-ヿ]", text):
            return "ja"
        if re.search(r"[가-힯]", text):
            return "ko"
        return "zh-CN"
    return "en"
