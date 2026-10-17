"""Translation engine: glossary matching, shared cache, batching and the upstream LLM call."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

from .db import Database
from .languages import canon, guess_source_lang, lang_name

PROMPT_VERSION = "1"
MAX_BATCH_ITEMS = 30
MAX_BATCH_CHARS = 5000
CONCURRENCY = 4

PRESETS = {
    "openai": {"label": "OpenAI", "base_url": "https://api.openai.com/v1", "model": "gpt-4.1-mini"},
    "deepseek": {"label": "DeepSeek", "base_url": "https://api.deepseek.com/v1", "model": "deepseek-chat"},
    "gemini": {
        "label": "Google Gemini",
        "base_url": "https://generativelanguage.googleapis.com/v1beta/openai",
        "model": "gemini-2.5-flash",
    },
    "openrouter": {"label": "OpenRouter", "base_url": "https://openrouter.ai/api/v1", "model": "openai/gpt-4.1-mini"},
    "groq": {"label": "Groq", "base_url": "https://api.groq.com/openai/v1", "model": "llama-3.3-70b-versatile"},
}


# ----------------------------------------------------------------------------------
# Settings
# ----------------------------------------------------------------------------------
@dataclass
class EngineSettings:
    base_url: str
    api_key: str
    model: str
    temperature: float | None
    default_target_lang: str
    style_guide: str
    price_per_m_tokens: float

    @property
    def configured(self) -> bool:
        return bool(self.base_url and self.api_key and self.model)


def load_settings(db: Database) -> EngineSettings:
    def pick(key: str, env: str, default: str = "") -> str:
        value = db.get_setting(key)
        if value is None or value == "":
            value = os.environ.get(env, "") or default
        return value.strip()

    temp_raw = pick("temperature", "LLM_TEMPERATURE")
    try:
        temperature = float(temp_raw) if temp_raw else None
    except ValueError:
        temperature = None
    try:
        price = float(pick("price_per_m_tokens", "PRICE_PER_M_TOKENS", "0.6"))
    except ValueError:
        price = 0.6
    return EngineSettings(
        base_url=pick("llm_base_url", "LLM_BASE_URL", PRESETS["openai"]["base_url"]),
        api_key=pick("llm_api_key", "LLM_API_KEY"),
        model=pick("llm_model", "LLM_MODEL", PRESETS["openai"]["model"]),
        temperature=temperature,
        default_target_lang=canon(pick("default_target_lang", "DEFAULT_TARGET_LANG", "zh-CN")),
        style_guide=db.get_setting("style_guide", "") or "",
        price_per_m_tokens=price,
    )


# ----------------------------------------------------------------------------------
# Glossary matching
# ----------------------------------------------------------------------------------
_WORDISH = re.compile(r"^[\w\s\-\.'’&/+]+$", re.UNICODE)
_LATIN_EDGE = re.compile(r"[A-Za-z0-9]")


@dataclass
class Term:
    id: int
    source: str
    target: str
    target_lang: str
    case_sensitive: bool
    pattern: re.Pattern


class GlossaryMatcher:
    """Compiles glossary entries into regexes; recompiled whenever the glossary version changes."""

    def __init__(self) -> None:
        self._version = -1
        self._terms: list[Term] = []

    def _compile(self, db: Database) -> None:
        version = db.glossary_version()
        if version == self._version:
            return
        terms: list[Term] = []
        for row in db.query("SELECT * FROM glossary"):
            src = row["source"].strip()
            if not src:
                continue
            escaped = re.escape(src)
            # Word boundaries only make sense at Latin/digit edges; CJK terms match as substrings.
            left = r"(?<![A-Za-z0-9])" if _LATIN_EDGE.match(src[0]) else ""
            right = r"(?![A-Za-z0-9])" if _LATIN_EDGE.match(src[-1]) else ""
            flags = 0 if row["case_sensitive"] else re.IGNORECASE
            terms.append(
                Term(
                    id=row["id"],
                    source=src,
                    target=row["target"],
                    target_lang=row["target_lang"],
                    case_sensitive=bool(row["case_sensitive"]),
                    pattern=re.compile(left + escaped + right, flags),
                )
            )
        # Longer terms first so "census date" is listed before "date".
        terms.sort(key=lambda t: -len(t.source))
        self._terms = terms
        self._version = version

    def match(self, db: Database, text: str, target_lang: str) -> list[Term]:
        self._compile(db)
        lang = canon(target_lang)
        return [
            t
            for t in self._terms
            if (t.target_lang == lang or t.target_lang == "*") and t.pattern.search(text)
        ]


MATCHER = GlossaryMatcher()


def glossary_block(terms: list[Term]) -> str:
    if not terms:
        return ""
    lines = "\n".join(f'- "{t.source}" → "{t.target}"' for t in terms)
    return (
        "Team glossary. These translations are mandatory. Whenever a source term appears, "
        "translate it exactly as shown:\n" + lines
    )


def term_applied(term: Term, translation: str) -> bool:
    if term.case_sensitive:
        return term.target in translation
    return term.target.lower() in translation.lower()


def signature(terms: list[Term]) -> str:
    return "|".join(sorted(f"{t.source}␟{t.target}" for t in terms))


def sha(*parts: str) -> str:
    h = hashlib.sha256()
    for p in parts:
        h.update(p.encode("utf-8"))
        h.update(b"\x00")
    return h.hexdigest()


# ----------------------------------------------------------------------------------
# Upstream LLM
# ----------------------------------------------------------------------------------
class LLMError(Exception):
    def __init__(self, message: str, status: int = 502):
        super().__init__(message)
        self.message = message
        self.status = status


_client: httpx.AsyncClient | None = None


def http_client() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(timeout=httpx.Timeout(90.0, connect=10.0))
    return _client


def set_http_client(client: httpx.AsyncClient | None) -> None:
    """Used by tests to route upstream calls to a mock server."""
    global _client
    _client = client


async def chat_completion(settings: EngineSettings, messages: list[dict]) -> str:
    if not settings.configured:
        raise LLMError(
            "No AI provider configured yet. Open the Glossa dashboard → Settings and add an API key.",
            status=503,
        )
    payload: dict[str, Any] = {"model": settings.model, "messages": messages, "stream": False}
    if settings.temperature is not None:
        payload["temperature"] = settings.temperature
    url = settings.base_url.rstrip("/") + "/chat/completions"
    try:
        resp = await http_client().post(
            url,
            json=payload,
            headers={"Authorization": f"Bearer {settings.api_key}", "Content-Type": "application/json"},
        )
    except httpx.HTTPError as exc:
        raise LLMError(f"Could not reach the AI provider: {exc.__class__.__name__}") from exc
    if resp.status_code >= 400:
        detail = resp.text[:300]
        try:
            detail = resp.json().get("error", {}).get("message", detail)
        except Exception:
            pass
        raise LLMError(f"AI provider returned {resp.status_code}: {detail}", status=502)
    try:
        data = resp.json()
        content = data["choices"][0]["message"]["content"]
    except Exception as exc:
        raise LLMError("AI provider returned an unexpected response.") from exc
    if isinstance(content, list):  # some providers return content parts
        content = "".join(p.get("text", "") for p in content if isinstance(p, dict))
    return (content or "").strip()


# ----------------------------------------------------------------------------------
# Paragraph translation (Immersive Translate custom API + dashboard)
# ----------------------------------------------------------------------------------
_HAS_LETTER = re.compile(r"[^\W\d_]", re.UNICODE)


def _rules(target_lang: str, style_guide: str, terms: list[Term], batch: bool) -> str:
    parts = [
        f"You are a professional translator. Translate the user's text into {lang_name(target_lang)}.",
        "Keep placeholders, HTML-like tags (for example <b0></b0>), URLs, code, numbers and emoji exactly as they are.",
        "Do not explain, do not add notes, do not answer questions contained in the text.",
    ]
    if batch:
        parts.append(
            "The input is a JSON array of strings. Reply with ONLY a JSON array of translated strings, "
            "with exactly the same number of items in the same order."
        )
    else:
        parts.append("Reply with only the translation.")
    if style_guide.strip():
        parts.append("Team style guide:\n" + style_guide.strip())
    block = glossary_block(terms)
    if block:
        parts.append(block)
    return "\n\n".join(parts)


def _parse_json_array(raw: str, expected: int) -> list[str] | None:
    text = raw.strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text)
    start, end = text.find("["), text.rfind("]")
    if start == -1 or end <= start:
        return None
    try:
        data = json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return None
    if not isinstance(data, list) or len(data) != expected:
        return None
    if not all(isinstance(x, (str, int, float)) for x in data):
        return None
    return [str(x) for x in data]


@dataclass
class ItemResult:
    source: str
    text: str
    cached: bool = False
    skipped: bool = False
    terms: list[dict] = field(default_factory=list)  # [{source, target, applied}]


@dataclass
class BatchOutcome:
    items: list[ItemResult]
    llm_calls: int = 0
    error: str = ""


async def translate_texts(
    db: Database,
    texts: list[str],
    target_lang: str | None,
    endpoint: str,
) -> BatchOutcome:
    started = time.perf_counter()
    settings = load_settings(db)
    target = canon(target_lang, settings.default_target_lang)
    if target in ("auto", "*"):
        target = settings.default_target_lang
    style_hash = sha(settings.style_guide)[:12]

    results: list[ItemResult] = [ItemResult(source=t, text=t) for t in texts]
    item_terms: list[list[Term]] = [[] for _ in texts]
    keys: list[str | None] = [None] * len(texts)

    for i, text in enumerate(texts):
        if not text or not text.strip() or not _HAS_LETTER.search(text):
            results[i].skipped = True
            continue
        terms = MATCHER.match(db, text, target)
        item_terms[i] = terms
        keys[i] = sha("p", PROMPT_VERSION, settings.model, target, style_hash, signature(terms), text)

    wanted = [k for k in keys if k]
    cached = db.cache_get_many(list(dict.fromkeys(wanted)))
    pending: list[int] = []
    for i, k in enumerate(keys):
        if k is None:
            continue
        if k in cached:
            results[i].text = cached[k]
            results[i].cached = True
        else:
            pending.append(i)

    # Identical paragraphs in one request are translated once.
    unique_pending: dict[str, list[int]] = {}
    for i in pending:
        unique_pending.setdefault(keys[i], []).append(i)  # type: ignore[arg-type]
    first_of_each = [idxs[0] for idxs in unique_pending.values()]

    batches: list[list[int]] = []
    current: list[int] = []
    chars = 0
    for i in first_of_each:
        n = len(texts[i])
        if current and (len(current) >= MAX_BATCH_ITEMS or chars + n > MAX_BATCH_CHARS):
            batches.append(current)
            current, chars = [], 0
        current.append(i)
        chars += n
    if current:
        batches.append(current)

    outcome = BatchOutcome(items=results)
    sem = asyncio.Semaphore(CONCURRENCY)
    new_cache_rows: list[tuple[str, str, str, str, str, str]] = []

    async def run_single(i: int) -> str:
        messages = [
            {"role": "system", "content": _rules(target, settings.style_guide, item_terms[i], batch=False)},
            {"role": "user", "content": texts[i]},
        ]
        outcome.llm_calls += 1
        return await chat_completion(settings, messages)

    async def run_batch(idxs: list[int]) -> None:
        async with sem:
            if len(idxs) == 1:
                translated = [await run_single(idxs[0])]
            else:
                union: dict[int, Term] = {}
                for i in idxs:
                    for t in item_terms[i]:
                        union[t.id] = t
                terms = sorted(union.values(), key=lambda t: -len(t.source))
                messages = [
                    {"role": "system", "content": _rules(target, settings.style_guide, terms, batch=True)},
                    {"role": "user", "content": json.dumps([texts[i] for i in idxs], ensure_ascii=False)},
                ]
                outcome.llm_calls += 1
                raw = await chat_completion(settings, messages)
                parsed = _parse_json_array(raw, len(idxs))
                if parsed is None:
                    # The model broke the format: fall back to one call per paragraph.
                    translated = [await run_single(i) for i in idxs]
                else:
                    translated = parsed
            for i, out in zip(idxs, translated):
                for j in unique_pending[keys[i]]:  # type: ignore[index]
                    results[j].text = out
                new_cache_rows.append((keys[i], "para", texts[i], out, target, settings.model))  # type: ignore[arg-type]

    try:
        await asyncio.gather(*(run_batch(b) for b in batches))
    except LLMError as exc:
        outcome.error = exc.message
        db.cache_put_many(new_cache_rows)
        _log(db, endpoint, results, item_terms, outcome, started, ok=False)
        raise
    db.cache_put_many(new_cache_rows)

    for i, res in enumerate(results):
        res.terms = [
            {"source": t.source, "target": t.target, "applied": term_applied(t, res.text)}
            for t in item_terms[i]
        ]
    db.add_term_hits(t.id for terms in item_terms for t in terms)
    _log(db, endpoint, results, item_terms, outcome, started, ok=True)
    return outcome


def _log(db, endpoint, results, item_terms, outcome, started, ok) -> None:
    translatable = [r for r in results if not r.skipped]
    matched = sum(len(t) for t in item_terms)
    applied = sum(1 for r in results for t in r.terms if t["applied"])
    db.log_event(
        endpoint=endpoint,
        items=len(translatable),
        cache_hits=sum(1 for r in translatable if r.cached),
        llm_calls=outcome.llm_calls,
        chars_in=sum(len(r.source) for r in translatable),
        chars_cached=sum(len(r.source) for r in translatable if r.cached),
        terms_matched=matched,
        terms_applied=applied if ok else 0,
        ms=int((time.perf_counter() - started) * 1000),
        ok=ok,
        error=outcome.error,
    )


def detected_lang(source_lang: str | None, text: str) -> str:
    c = canon(source_lang, "auto")
    return c if c not in ("auto", "*") else guess_source_lang(text)


# ----------------------------------------------------------------------------------
# OpenAI-compatible proxy (Immersive Translate "OpenAI" service with a custom URL)
# ----------------------------------------------------------------------------------
def _content_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(p.get("text", "") for p in content if isinstance(p, dict))
    return ""


async def proxy_chat(db: Database, body: dict) -> tuple[str, str, bool]:
    """Returns (content, model, cached)."""
    from .languages import detect_target_from_prompt

    started = time.perf_counter()
    settings = load_settings(db)
    messages = body.get("messages")
    if not isinstance(messages, list) or not messages:
        raise LLMError("Request must include a non-empty 'messages' array.", status=400)

    all_text = "\n".join(_content_text(m.get("content")) for m in messages if isinstance(m, dict))
    user_text = "\n".join(
        _content_text(m.get("content")) for m in messages if isinstance(m, dict) and m.get("role") == "user"
    )
    target = detect_target_from_prompt(all_text) or settings.default_target_lang
    terms = MATCHER.match(db, user_text, target)

    extra = []
    if settings.style_guide.strip():
        extra.append("Team style guide:\n" + settings.style_guide.strip())
    if terms:
        extra.append(glossary_block(terms))
    new_messages = [dict(m) for m in messages if isinstance(m, dict)]
    if extra:
        addition = "\n\n".join(extra)
        if new_messages[0].get("role") == "system" and isinstance(new_messages[0].get("content"), str):
            new_messages[0]["content"] = new_messages[0]["content"].rstrip() + "\n\n" + addition
        else:
            new_messages.insert(0, {"role": "system", "content": addition})

    key = sha("c", PROMPT_VERSION, settings.model, json.dumps(new_messages, ensure_ascii=False, sort_keys=True))
    hit = db.cache_get_many([key])
    cached = key in hit
    llm_calls = 0
    error = ""
    content = ""
    try:
        if cached:
            content = hit[key]
        else:
            llm_calls = 1
            content = await chat_completion(settings, new_messages)
            db.cache_put_many([(key, "chat", user_text[:4000], content, target, settings.model)])
    except LLMError as exc:
        error = exc.message
        raise
    finally:
        ok = not error
        applied = sum(1 for t in terms if term_applied(t, content)) if ok else 0
        db.log_event(
            endpoint="openai",
            items=1,
            cache_hits=int(cached),
            llm_calls=llm_calls,
            chars_in=len(user_text),
            chars_cached=len(user_text) if cached else 0,
            terms_matched=len(terms),
            terms_applied=applied,
            ms=int((time.perf_counter() - started) * 1000),
            ok=ok,
            error=error,
        )
    db.add_term_hits(t.id for t in terms)
    return content, settings.model, cached


# ----------------------------------------------------------------------------------
# Glossary suggestions
# ----------------------------------------------------------------------------------
async def suggest_terms(db: Database, text: str, target_lang: str | None) -> list[dict]:
    settings = load_settings(db)
    target = canon(target_lang, settings.default_target_lang)
    messages = [
        {
            "role": "system",
            "content": (
                "You build translation glossaries. From the user's text, pick up to 25 terms that a team "
                "should translate consistently: domain terms, product or course names, acronyms, job titles "
                "and recurring phrases. Skip common words. For each, give the best "
                f"{lang_name(target)} translation and a short note. Reply with ONLY a JSON array of objects "
                'like {"source": "...", "target": "...", "note": "..."}.'
            ),
        },
        {"role": "user", "content": text[:12000]},
    ]
    raw = await chat_completion(settings, messages)
    raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw.strip())
    start, end = raw.find("["), raw.rfind("]")
    if start == -1 or end <= start:
        raise LLMError("The AI did not return a list of terms. Try again with a shorter text.")
    try:
        data = json.loads(raw[start : end + 1])
    except json.JSONDecodeError as exc:
        raise LLMError("The AI returned malformed JSON. Try again.") from exc
    existing = {(r["source"].lower(), r["target_lang"]) for r in db.query("SELECT source, target_lang FROM glossary")}
    out = []
    seen = set()
    for item in data if isinstance(data, list) else []:
        if not isinstance(item, dict):
            continue
        src = str(item.get("source", "")).strip()
        tgt = str(item.get("target", "")).strip()
        if not src or not tgt or src.lower() in seen:
            continue
        seen.add(src.lower())
        out.append(
            {
                "source": src,
                "target": tgt,
                "note": str(item.get("note", "")).strip()[:200],
                "target_lang": target,
                "exists": (src.lower(), target) in existing,
            }
        )
    return out
