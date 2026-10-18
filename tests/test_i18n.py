"""The interface must be complete in every language: no missing keys, no untranslated error codes."""

import json
import re
from pathlib import Path

from app.errors import ALL_CODES

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "app" / "static"
I18N = json.loads((STATIC / "i18n.json").read_text(encoding="utf-8"))
LANGS = ["en", "zh-CN", "zh-TW", "ja", "ko", "vi"]
NAMESPACES = ("lang", "auth", "setup", "login", "nav", "overview", "chart", "terms", "connect", "invite", "toast",
              "recent", "source", "glossary", "example", "suggest", "try", "highlight", "settings", "err",
              "warn", "history", "proposal", "member")


def test_every_language_has_every_key():
    en = I18N["en"]
    base = {k for k in en if not k.endswith("_one")}
    for lang in LANGS:
        keys = set(I18N[lang])
        assert base <= keys, (lang, sorted(base - keys))
        assert keys <= set(en), (lang, sorted(keys - set(en)))
        assert all(isinstance(v, str) and v.strip() for v in I18N[lang].values()), lang
    assert set(I18N["_meta"]["names"]) == set(LANGS)


def test_placeholders_match_english():
    en = I18N["en"]
    ph = re.compile(r"\{(\w+)\}")
    for lang in LANGS:
        for key, text in I18N[lang].items():
            expected = set(ph.findall(en[key] if key in en else en[key.removesuffix("_one")]))
            assert set(ph.findall(text)) == expected, (lang, key, text)


def test_keys_used_in_the_page_exist():
    html = "".join((STATIC / f).read_text(encoding="utf-8") for f in ("index.html", "suggest.html"))
    js = "".join((STATIC / f).read_text(encoding="utf-8") for f in ("i18n.js", "app.js", "member.js"))
    used = set(re.findall(r'data-i18n(?:-html|-ph|-aria|-title)?="([\w.]+)"', html))
    used |= {k for k in re.findall(r'"([a-z]+\.[a-z0-9_]+)"', js) if k.split(".")[0] in NAMESPACES}
    used |= {f"source.{s}" for s in ("immersive", "openai", "dashboard", "member")}
    used |= {f"warn.{w}" for w in ("short", "common", "broad")}  # built from the server's warning codes
    missing = sorted(k for k in used if k not in I18N["en"])
    assert not missing, missing
    assert len(used) > 220


def test_every_error_code_is_translated_and_declared():
    for code in ALL_CODES:
        assert f"err.{code}" in I18N["en"], code
    source = "\n".join(p.read_text(encoding="utf-8") for p in (ROOT / "app").glob("*.py"))
    found = set(re.findall(r'AppError\(\d+, "(\w+)"', source))
    found |= set(re.findall(r'code="(\w+)"', source))
    found |= set(re.findall(r'LLMError\([^)]*?, \d{3}, "(\w+)"', source, re.S))
    found |= set(re.findall(r'\d{3}, "(upstream_\w+)"', source))
    assert found, "no codes found"
    assert found <= set(ALL_CODES), sorted(found - set(ALL_CODES))


def test_non_english_text_has_no_stray_english_words():
    # Brand and technical names are allowed; ordinary English words would mean an untranslated string.
    allowed = {"glossa", "openai", "api", "key", "apikey", "csv", "token", "unsw", "zenith", "admin_password",
               "comp2521", "immersive", "translate", "beta", "aa", "usd", "gl", "oss", "a", "b", "strong",
               "span", "class", "muted", "from", "memory", "myunsw", "ai"}
    word = re.compile(r"[A-Za-z][A-Za-z0-9_]+")
    for lang in ("zh-CN", "zh-TW", "ja", "ko"):
        for key, text in I18N[lang].items():
            plain = re.sub(r"\{\w+\}", "", text)
            stray = {w.lower() for w in word.findall(plain)} - allowed
            assert not stray, (lang, key, stray)
