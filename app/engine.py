"""Translation engine.

Every paragraph goes through the same pipeline, whichever endpoint it arrived on:

  glossary match ─► exact-term shortcut ─► shared memory (cache) ─► dispatcher ─► AI provider

The dispatcher merges paragraphs that arrive close together (from one or many requests) into a
few upstream calls, keeps the call rate under the configured requests-per-minute, retries on
rate limits and outages, and checks that glossary terms were used.
"""

from __future__ import annotations

import asyncio
import hashlib
import itertools
import json
import logging
import os
import re
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

import httpx

from .db import Database
from .errors import LLMError, from_upstream_status
from .languages import canon, detect_target_from_prompt, guess_source_lang, lang_name

log = logging.getLogger("uvicorn.error")

PROMPT_VERSION = "1"
MAX_BATCH_ITEMS = 40
MAX_BATCH_CHARS = 6000
CONCURRENCY = 4
COALESCE_SECONDS = 0.35   # how long the dispatcher waits for more paragraphs before calling the AI
MAX_ATTEMPTS = 3          # first try + 2 retries
MAX_RETRY_WAIT = 40.0     # never make a client wait longer than this for one retry
DEFAULT_MAX_RPM = 12

PRESETS = {
    "openai": {"label": "OpenAI", "base_url": "https://api.openai.com/v1", "model": "gpt-4.1-mini"},
    "deepseek": {"label": "DeepSeek", "base_url": "https://api.deepseek.com/v1", "model": "deepseek-chat"},
    "gemini": {
        "label": "Google Gemini",
        "base_url": "https://generativelanguage.googleapis.com/v1beta/openai",
        "model": "gemini-3.5-flash-lite",
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
    max_rpm: int

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
    try:
        max_rpm = max(0, int(float(pick("max_rpm", "MAX_RPM", str(DEFAULT_MAX_RPM)))))
    except ValueError:
        max_rpm = DEFAULT_MAX_RPM
    return EngineSettings(
        base_url=pick("llm_base_url", "LLM_BASE_URL", PRESETS["openai"]["base_url"]),
        api_key=pick("llm_api_key", "LLM_API_KEY"),
        model=pick("llm_model", "LLM_MODEL", PRESETS["openai"]["model"]),
        temperature=temperature,
        default_target_lang=canon(pick("default_target_lang", "DEFAULT_TARGET_LANG", "zh-CN")),
        style_guide=db.get_setting("style_guide", "") or "",
        price_per_m_tokens=price,
        max_rpm=max_rpm,
    )


# ----------------------------------------------------------------------------------
# Glossary matching
# ----------------------------------------------------------------------------------
_LATIN_EDGE = re.compile(r"[A-Za-z0-9]")
_SEPARATORS = r"[\s\-‐‑‒–—_/]+"   # spaces (incl. no-break), hyphens, dashes
_TRAILING_PUNCT = ":：.。!！?？,，;；"


def _word_pattern(word: str, last: bool) -> str:
    """A regex for one word of a term. The last English word may take a plural or possessive ending."""
    escaped = re.escape(word)
    if not last or not re.fullmatch(r"[A-Za-z]{2,}", word):
        return escaped
    if len(word) > 2 and word[-1] in "yY" and word[-2].lower() not in "aeiou":
        stem = re.escape(word[:-1])
        return stem + r"(?:y|ies|y's|y’s)"
    return escaped + r"(?:s|es|'s|’s)?"


def term_core(source: str) -> str:
    words = [w for w in re.split(_SEPARATORS, source.strip()) if w]
    if not words:
        return re.escape(source)
    if len(words) == 1 and not _LATIN_EDGE.match(words[0][0]):
        return re.escape(words[0])  # CJK and other scripts: literal match
    parts = [_word_pattern(w, i == len(words) - 1) for i, w in enumerate(words)]
    return _SEPARATORS.join(parts)


def compile_term(source: str, case_sensitive: bool) -> tuple[re.Pattern, re.Pattern]:
    """(pattern that finds the term inside a paragraph, pattern for a paragraph that is only the term)."""
    src = source.strip()
    core = term_core(src)
    # Word boundaries only make sense at Latin/digit edges; CJK terms match as substrings.
    left = r"(?<![A-Za-z0-9])" if _LATIN_EDGE.match(src[0]) else ""
    right = r"(?![A-Za-z0-9])" if _LATIN_EDGE.match(src[-1]) else ""
    flags = 0 if case_sensitive else re.IGNORECASE
    return re.compile(left + core + right, flags), re.compile(core, flags)


@dataclass
class Term:
    id: int
    source: str
    target: str
    target_lang: str
    case_sensitive: bool
    pattern: re.Pattern      # finds the term inside a paragraph
    whole: re.Pattern        # matches a paragraph that is only the term
    note: str = ""           # sent to the AI with the term
    conditional: bool = False  # the AI decides from the note whether the term applies; never forced
    set_id: int = 1
    rank: tuple = ()         # set priority: lower wins when a link's sets share a term


class TermView:
    """The terms one link uses for one target language: enabled terms in the link's sets, where a
    term that appears in several sets comes from the highest-priority set."""

    def __init__(self, terms: list[Term]):
        self.terms = terms

    def match(self, text: str) -> list[Term]:
        return [t for t in self.terms if t.pattern.search(text)]

    def exact(self, text: str) -> Term | None:
        """The term, if the whole paragraph is just that term (menu items, labels, headings)."""
        stripped = text.strip().rstrip(_TRAILING_PUNCT).strip()
        if not stripped or len(stripped) > 200:
            return None
        for t in self.terms:
            if t.conditional:
                continue  # whether it applies depends on the meaning, so the AI decides
            if t.whole.fullmatch(stripped):
                return t
        return None


class GlossaryMatcher:
    """Compiles glossary entries into regexes; recompiled whenever the glossary version changes."""

    def __init__(self) -> None:
        self._version = -1
        self._terms: list[Term] = []
        self._views: dict[tuple, TermView] = {}

    def _compile(self, db: Database) -> None:
        version = db.glossary_version()
        if version == self._version:
            return
        terms: list[Term] = []
        rows = db.query(
            "SELECT g.*, s.position AS set_position FROM glossary g "
            "JOIN glossary_sets s ON s.id = g.set_id WHERE g.enabled = 1"
        )
        for row in rows:
            src = row["source"].strip()
            if not src:
                continue
            pattern, whole = compile_term(src, bool(row["case_sensitive"]))
            terms.append(
                Term(
                    id=row["id"],
                    source=src,
                    target=row["target"],
                    target_lang=row["target_lang"],
                    case_sensitive=bool(row["case_sensitive"]),
                    pattern=pattern,
                    whole=whole,
                    note=(row.get("note") or "").strip(),
                    conditional=bool(row.get("conditional")),
                    set_id=row["set_id"],
                    rank=(row["set_id"] == 1, row["set_position"], row["set_id"]),
                )
            )
        self._terms = terms
        self._views = {}
        self._version = version

    def view(self, db: Database, target_lang: str, set_ids: list[int]) -> TermView:
        self._compile(db)
        lang = canon(target_lang)
        key = (lang, tuple(set_ids))
        if key not in self._views:
            allowed = set(set_ids)
            best: dict[str, Term] = {}
            for t in self._terms:
                if t.set_id not in allowed or (t.target_lang != lang and t.target_lang != "*"):
                    continue
                k = t.source.lower()
                if k not in best or t.rank < best[k].rank:
                    best[k] = t
            # Longer terms first: "census date" before "date".
            self._views[key] = TermView(sorted(best.values(), key=lambda t: -len(t.source)))
        return self._views[key]


MATCHER = GlossaryMatcher()


def _one_line(text: str) -> str:
    return " ".join(text.split())


def glossary_block(terms: list[Term], strict: bool = False) -> str:
    """Mandatory terms first (with their notes as context), then terms whose use depends on the meaning."""
    fixed = [t for t in terms if not t.conditional]
    depends = [t for t in terms if t.conditional]
    parts = []
    if fixed:
        lines = "\n".join(
            f'- "{t.source}" → "{t.target}"' + (f" (note: {_one_line(t.note)})" if t.note else "") for t in fixed
        )
        intro = (
            "Team glossary. These translations are mandatory. Whenever a source term appears "
            "(including plural or possessive forms), translate it exactly as shown:\n"
        )
        if strict:
            intro = (
                "Team glossary. A previous translation ignored these terms. Use each translation below "
                "exactly, character for character, wherever its source term appears:\n"
            )
        parts.append(intro + lines)
    if depends:
        lines = "\n".join(
            f'- "{t.source}" → "{t.target}" (applies when: {_one_line(t.note) or "the context clearly fits"})'
            for t in depends
        )
        parts.append(
            "Context-dependent glossary terms. Use the given translation only where the condition applies; "
            "elsewhere translate the word normally:\n" + lines
        )
    return "\n\n".join(parts)


def term_applied(term: Term, translation: str) -> bool:
    if term.case_sensitive:
        return term.target in translation
    return term.target.lower() in translation.lower()


def signature(terms: list[Term]) -> str:
    """Part of the memory key. A term's note and mode only appear when set, so paragraphs cached
    before notes were sent to the AI keep their keys."""
    def one(t: Term) -> str:
        s = f"{t.source}␟{t.target}"
        if t.note:
            s += f"␟{t.note}"
        if t.conditional:
            s += "␟ctx"
        return s
    return "|".join(sorted(one(t) for t in terms))


# ----------------------------------------------------------------------------------
# Checks before a term is saved
# ----------------------------------------------------------------------------------
# Everyday words that are rarely worth forcing one translation on. Not exhaustive: the memory
# check below catches site-specific words too.
COMMON_WORDS = frozenset("""
a about after again all also an and any are as at back be because been before being below between both but by
can could day days did do does done down during each even every few first for from get go good had has have he
her here him his how i if in into is it its just key last like make many may me more most much must my new
next no not now of off on once one only or other our out over own page part people please read same see she
should so some such take than that the their them then there these they this those through time to too under
until up us use used very was way we week weeks well were what when where which while who why will with would
year years yes yet you your home news date dates term terms information info about more contact click here
search menu link links back top view show open close start end note notes item items name number type list
""".split())


def static_warnings(source: str, case_sensitive: bool) -> list[str]:
    """Warnings that need no data: very short terms and everyday words."""
    src = source.strip()
    warnings = []
    latin = bool(_LATIN_EDGE.match(src[:1] or " "))
    if (latin and len(src) <= 3 and not case_sensitive) or (not latin and len(src) <= 1):
        warnings.append("short")
    if latin and " " not in src and src.lower() in COMMON_WORDS:
        warnings.append("common")
    return warnings


def term_warnings(db: Database, source: str, target_lang: str, case_sensitive: bool) -> dict:
    """What saving this term would affect: static warnings, plus how much of the shared memory it
    matches. A term that matches a large share of everything already translated is probably too broad."""
    warnings = static_warnings(source, case_sensitive)
    pattern, _ = compile_term(source, case_sensitive)
    rows = db.query(
        "SELECT source_text FROM cache WHERE kind = 'para' AND target_lang = ? ORDER BY created_at DESC LIMIT 3000",
        (canon(target_lang),),
    )
    hits = [r["source_text"] for r in rows if pattern.search(r["source_text"])]
    total = len(rows)
    if total >= 10 and len(hits) / total >= 0.2 and len(hits) >= 5:
        warnings.append("broad")
    samples = [h if len(h) <= 140 else h[:137] + "…" for h in hits[:2]]
    return {"warnings": warnings, "affected": len(hits), "total": total, "samples": samples}


def sha(*parts: str) -> str:
    h = hashlib.sha256()
    for p in parts:
        h.update(p.encode("utf-8"))
        h.update(b"\x00")
    return h.hexdigest()


# ----------------------------------------------------------------------------------
# Upstream HTTP
# ----------------------------------------------------------------------------------
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


def _parse_duration(value: Any) -> float | None:
    try:
        text = str(value).strip().lower()
        return float(text[:-1]) if text.endswith("s") else float(text)
    except (TypeError, ValueError):
        return None


def parse_error_body(resp: httpx.Response) -> tuple[str, float | None, str]:
    """Returns (message, retry_after_seconds, raw_text). Handles OpenAI-style objects and Google's
    list-wrapped errors such as [{"error": {"code": 429, "message": ..., "details": [...]}}]."""
    raw = resp.text[:4000]
    retry_after = _parse_duration(resp.headers.get("retry-after")) if resp.headers.get("retry-after") else None
    message = raw
    try:
        data: Any = resp.json()
    except ValueError:
        return message, retry_after, raw
    if isinstance(data, list) and data:
        data = data[0]
    err = data.get("error") if isinstance(data, dict) else None
    if isinstance(err, dict):
        message = str(err.get("message") or raw)
        for detail in err.get("details") or []:
            if isinstance(detail, dict) and detail.get("retryDelay") is not None:
                retry_after = _parse_duration(detail["retryDelay"]) or retry_after
    elif isinstance(err, str):
        message = err
    return message, retry_after, raw


async def chat_completion(settings: EngineSettings, messages: list[dict]) -> str:
    """One raw call to the provider. No retries here; see call_upstream."""
    if not settings.configured:
        raise LLMError(
            "No AI provider configured yet. Open the Glossa dashboard → Settings and add an API key.",
            status=503, code="not_configured",
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
    except httpx.TimeoutException as exc:
        raise LLMError("The AI provider took too long to answer.", 504, "upstream_unreachable",
                       retryable=True) from exc
    except httpx.HTTPError as exc:
        raise LLMError(f"Could not reach the AI provider ({exc.__class__.__name__}).", 502,
                       "upstream_unreachable", retryable=True) from exc
    if resp.status_code >= 400:
        message, retry_after, raw = parse_error_body(resp)
        raise from_upstream_status(
            resp.status_code, f"AI provider returned {resp.status_code}: {message}", raw, retry_after
        )
    try:
        data = resp.json()
        content = data["choices"][0]["message"]["content"]
    except Exception as exc:
        raise LLMError("The AI provider returned an unexpected response.", 502, "upstream_bad_response",
                       raw=resp.text[:2000]) from exc
    if isinstance(content, list):  # some providers return content parts
        content = "".join(p.get("text", "") for p in content if isinstance(p, dict))
    return (content or "").strip()


# ----------------------------------------------------------------------------------
# Rate limiting and retries
# ----------------------------------------------------------------------------------
class RateLimiter:
    """Allows at most `rpm` calls in any 60-second window. Waiters are served in order.
    `pause_for` makes everyone wait, used when the provider says it is rate limiting us."""

    def __init__(self, clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], Awaitable[None]] = asyncio.sleep):
        self.clock = clock
        self.sleep = sleep
        self.calls: deque[float] = deque()
        self.paused_until = 0.0
        self._lock: asyncio.Lock | None = None

    def pause_for(self, seconds: float) -> None:
        self.paused_until = max(self.paused_until, self.clock() + seconds)

    async def acquire(self, rpm: int) -> float:
        """Waits for a slot; returns how long it waited."""
        if self._lock is None:
            self._lock = asyncio.Lock()
        waited = 0.0
        async with self._lock:
            while True:
                now = self.clock()
                if self.paused_until > now:
                    delay = self.paused_until - now
                elif rpm > 0:
                    while self.calls and now - self.calls[0] >= 60:
                        self.calls.popleft()
                    if len(self.calls) < rpm:
                        break
                    delay = 60 - (now - self.calls[0]) + 0.05
                else:
                    break
                waited += delay
                await self.sleep(delay)
            self.calls.append(self.clock())
        return waited

    def calls_in_last_minute(self) -> int:
        now = self.clock()
        return sum(1 for t in self.calls if now - t < 60)


async def call_upstream(db: Database, settings: EngineSettings, messages: list[dict], items: int,
                        limiter: RateLimiter, sem: asyncio.Semaphore) -> str:
    """A provider call with the shared rate limit, bounded concurrency and retries."""
    for attempt in range(1, MAX_ATTEMPTS + 1):
        await limiter.acquire(settings.max_rpm)
        started = time.perf_counter()
        async with sem:
            try:
                content = await chat_completion(settings, messages)
            except LLMError as exc:
                ms = int((time.perf_counter() - started) * 1000)
                if exc.code != "not_configured":
                    db.log_call(settings.model, items, exc.upstream, ms, False, exc.code)
                log.warning("glossa: AI call failed (attempt %d/%d, %s): %s",
                            attempt, MAX_ATTEMPTS, exc.code, exc.raw[:1500])
                wait = exc.retry_after if exc.retry_after is not None else 1.5 * (2 ** (attempt - 1))
                if exc.code == "upstream_rate_limit":
                    limiter.pause_for(min(wait, MAX_RETRY_WAIT))
                if not exc.retryable or attempt == MAX_ATTEMPTS or wait > MAX_RETRY_WAIT:
                    raise
                await asyncio.sleep(0 if exc.code == "upstream_rate_limit" else wait)  # limiter holds the pause
                continue
        ms = int((time.perf_counter() - started) * 1000)
        db.log_call(settings.model, items, 200, ms, True)
        log.info("glossa: AI call ok (%d paragraphs, %d ms, attempt %d)", items, ms, attempt)
        return content
    raise AssertionError("unreachable")


# ----------------------------------------------------------------------------------
# Prompts
# ----------------------------------------------------------------------------------
_P_MARK = re.compile(r"\[\[p(\d+)\]\]")


def _rules(target_lang: str, style_guide: str, terms: list[Term], strict: bool = False) -> str:
    parts = [
        f"You are a professional translator. Translate the user's text into {lang_name(target_lang)}.",
        "Keep placeholders, HTML-like tags (for example <b0></b0>), URLs, code, numbers and emoji exactly as they are.",
        "Do not explain, do not add notes, do not answer questions contained in the text.",
        "The input is a list of segments. Each segment starts with a marker on its own line, such as [[p0]]. "
        "Reply in exactly the same format: every marker on its own line, followed by that segment's translation. "
        "Keep every marker and the order. Do not merge or split segments and add nothing else.",
    ]
    if style_guide.strip():
        parts.append("Team style guide:\n" + style_guide.strip())
    block = glossary_block(terms, strict=strict)
    if block:
        parts.append(block)
    return "\n\n".join(parts)


def format_segments(texts: list[str]) -> str:
    return "\n".join(f"[[p{i}]]\n{t}" for i, t in enumerate(texts))


def parse_segments(raw: str, expected: int) -> list[str] | None:
    text = re.sub(r"^```[a-z]*\s*|\s*```$", "", raw.strip())
    text = text.replace("[[source_end]]", "")
    parts = _P_MARK.split(text)
    if len(parts) < 3:
        return None
    found: dict[int, str] = {}
    for i in range(1, len(parts), 2):
        found[int(parts[i])] = parts[i + 1].strip()
    if not all(i in found for i in range(expected)):
        return None
    return [found[i] for i in range(expected)]


# ----------------------------------------------------------------------------------
# Dispatcher: coalesces paragraphs into batched upstream calls
# ----------------------------------------------------------------------------------
@dataclass
class Job:
    key: str
    text: str
    target: str
    terms: list[Term]
    future: asyncio.Future


_batch_ids = itertools.count(1)


class Dispatcher:
    def __init__(self, loop: asyncio.AbstractEventLoop):
        self.loop = loop
        self.pending: dict[str, Job] = {}
        self.inflight: dict[str, asyncio.Future] = {}
        self.timer: asyncio.TimerHandle | None = None
        self.limiter = RateLimiter()
        self.sem = asyncio.Semaphore(CONCURRENCY)
        self.db: Database | None = None

    def submit(self, db: Database, key: str, text: str, target: str, terms: list[Term]) -> asyncio.Future:
        """Returns a future resolving to (translation, batch_id). Identical paragraphs share one future."""
        self.db = db
        if key in self.inflight:
            return self.inflight[key]
        fut = self.loop.create_future()
        self.inflight[key] = fut
        self.pending[key] = Job(key, text, target, terms, fut)
        if sum(len(j.text) for j in self.pending.values()) >= MAX_BATCH_CHARS * 2:
            self.flush()
        elif self.timer is None:
            self.timer = self.loop.call_later(COALESCE_SECONDS, self.flush)
        return fut

    def flush(self) -> None:
        if self.timer is not None:
            self.timer.cancel()
            self.timer = None
        jobs = list(self.pending.values())
        self.pending.clear()
        if not jobs or self.db is None:
            return
        by_target: dict[str, list[Job]] = {}
        for j in jobs:
            by_target.setdefault(j.target, []).append(j)
        for group in by_target.values():
            batch: list[Job] = []
            chars = 0
            for j in group:
                if batch and (len(batch) >= MAX_BATCH_ITEMS or chars + len(j.text) > MAX_BATCH_CHARS):
                    self.loop.create_task(self.run(self.db, batch))
                    batch, chars = [], 0
                batch.append(j)
                chars += len(j.text)
            if batch:
                self.loop.create_task(self.run(self.db, batch))

    async def run(self, db: Database, jobs: list[Job]) -> None:
        batch_id = next(_batch_ids)
        settings = load_settings(db)
        try:
            outs = await self.translate(db, settings, jobs)
            rows = []
            for job, out in zip(jobs, outs):
                if not job.future.done():
                    job.future.set_result((out, batch_id))
                rows.append((job.key, "para", job.text, out, job.target, settings.model))
            db.cache_put_many(rows)
        except LLMError as exc:
            for job in jobs:
                if not job.future.done():
                    job.future.set_exception(exc)
        except Exception as exc:  # never leave a request hanging
            log.exception("glossa: batch failed unexpectedly")
            err = LLMError(f"Internal error: {exc.__class__.__name__}", 500, "internal_error")
            for job in jobs:
                if not job.future.done():
                    job.future.set_exception(err)
        finally:
            for job in jobs:
                self.inflight.pop(job.key, None)
                if job.future.done() and not job.future.cancelled():
                    job.future.exception()  # mark as retrieved; requests read it themselves

    async def translate(self, db: Database, s: EngineSettings, jobs: list[Job],
                        strict: bool = False, check_terms: bool = True) -> list[str]:
        union: dict[int, Term] = {}
        for j in jobs:
            for t in j.terms:
                union[t.id] = t
        terms = sorted(union.values(), key=lambda t: -len(t.source))
        target = jobs[0].target
        messages = [
            {"role": "system", "content": _rules(target, s.style_guide, terms, strict=strict)},
            {"role": "user", "content": format_segments([j.text for j in jobs])},
        ]
        raw = await call_upstream(db, s, messages, len(jobs), self.limiter, self.sem)
        outs = parse_segments(raw, len(jobs))
        if outs is None:
            if len(jobs) == 1:
                outs = [_P_MARK.sub("", raw.replace("[[source_end]]", "")).strip()]
            else:
                # The model broke the format: split the batch in two and try each half.
                mid = len(jobs) // 2
                left = await self.translate(db, s, jobs[:mid], strict, check_terms)
                right = await self.translate(db, s, jobs[mid:], strict, check_terms)
                return left + right
        if check_terms:
            # Only mandatory terms are enforced; conditional ones are the AI's call by design.
            def must(job: Job) -> list[Term]:
                return [t for t in job.terms if not t.conditional]

            missed = [i for i, j in enumerate(jobs) if any(not term_applied(t, outs[i]) for t in must(j))]
            if missed:
                try:
                    fixed = await self.translate(db, s, [jobs[i] for i in missed], strict=True, check_terms=False)
                    for i, out in zip(missed, fixed):
                        if all(term_applied(t, out) for t in must(jobs[i])):
                            outs[i] = out
                except LLMError:
                    pass  # keep the first translation; compliance is reported in stats
        return outs


_dispatcher: Dispatcher | None = None


def get_dispatcher() -> Dispatcher:
    global _dispatcher
    loop = asyncio.get_running_loop()
    if _dispatcher is None or _dispatcher.loop is not loop:
        _dispatcher = Dispatcher(loop)
    return _dispatcher


def reset_dispatcher() -> None:
    global _dispatcher
    _dispatcher = None


async def call_once(db: Database, messages: list[dict], items: int = 1) -> str:
    """A single non-batched call (connection test, suggestions, unrecognised proxy formats)
    that still respects the shared rate limit and retries."""
    d = get_dispatcher()
    return await call_upstream(db, load_settings(db), messages, items, d.limiter, d.sem)


# ----------------------------------------------------------------------------------
# Paragraph translation (all endpoints)
# ----------------------------------------------------------------------------------
_HAS_LETTER = re.compile(r"[^\W\d_]", re.UNICODE)


@dataclass
class ItemResult:
    source: str
    text: str
    cached: bool = False
    direct: bool = False          # the paragraph was exactly a glossary term
    skipped: bool = False         # nothing to translate (numbers, whitespace)
    error: LLMError | None = None
    terms: list[dict] = field(default_factory=list)  # [{source, target, applied}]


@dataclass
class BatchOutcome:
    items: list[ItemResult]
    llm_calls: int = 0

    @property
    def first_error(self) -> LLMError | None:
        return next((i.error for i in self.items if i.error), None)


def default_sets(db: Database) -> list[int]:
    return db.link_set_ids(1)


async def translate_texts(db: Database, texts: list[str], target_lang: str | None, endpoint: str,
                          set_ids: list[int] | None = None) -> BatchOutcome:
    """Translates every paragraph it can. Failed paragraphs carry `error`; successful ones are
    saved to memory, so a retry only has to redo the failures. `set_ids` are the glossary sets of
    the link the request came through (the default link if None)."""
    started = time.perf_counter()
    settings = load_settings(db)
    target = canon(target_lang, settings.default_target_lang)
    if target in ("auto", "*"):
        target = settings.default_target_lang
    style_hash = sha(settings.style_guide)[:12]
    view = MATCHER.view(db, target, default_sets(db) if set_ids is None else set_ids)

    results = [ItemResult(source=t, text=t) for t in texts]
    item_terms: list[list[Term]] = [[] for _ in texts]
    keys: list[str | None] = [None] * len(texts)

    for i, text in enumerate(texts):
        if not text or not text.strip() or not _HAS_LETTER.search(text):
            results[i].skipped = True
            continue
        direct = view.exact(text)
        if direct is not None:
            stripped = text.strip()
            suffix = stripped[len(stripped.rstrip(_TRAILING_PUNCT)):]  # keep a trailing colon etc.
            results[i].text = direct.target + suffix
            results[i].direct = True
            item_terms[i] = [direct]
            continue
        terms = view.match(text)
        item_terms[i] = terms
        keys[i] = sha("p", PROMPT_VERSION, settings.model, target, style_hash, signature(terms), text)

    wanted = [k for k in keys if k]
    cached = db.cache_get_many(list(dict.fromkeys(wanted)))
    waiting: dict[str, asyncio.Future] = {}
    dispatcher = get_dispatcher() if any(k and k not in cached for k in keys) else None
    for i, k in enumerate(keys):
        if k is None:
            continue
        if k in cached:
            results[i].text = cached[k]
            results[i].cached = True
        elif k not in waiting:
            waiting[k] = dispatcher.submit(db, k, texts[i], target, item_terms[i])  # type: ignore[union-attr]

    batch_ids: set[int] = set()
    if waiting:
        done = await asyncio.gather(*waiting.values(), return_exceptions=True)
        resolved = dict(zip(waiting.keys(), done))
        for i, k in enumerate(keys):
            if k is None or results[i].cached:
                continue
            value = resolved[k]
            if isinstance(value, LLMError):
                results[i].error = value
            elif isinstance(value, BaseException):
                results[i].error = LLMError(f"Internal error: {value.__class__.__name__}", 500, "internal_error")
            else:
                results[i].text, batch_id = value
                batch_ids.add(batch_id)

    for i, res in enumerate(results):
        if res.error is None:
            res.terms = [
                {"source": t.source, "target": t.target, "applied": term_applied(t, res.text),
                 "conditional": t.conditional}
                for t in item_terms[i]
            ]
    db.add_term_hits(t.id for i, terms in enumerate(item_terms) if results[i].error is None for t in terms)
    outcome = BatchOutcome(items=results, llm_calls=len(batch_ids))
    _log(db, endpoint, outcome, item_terms, started)
    return outcome


def _log(db: Database, endpoint: str, outcome: BatchOutcome, item_terms: list[list[Term]], started: float) -> None:
    items = [r for r in outcome.items if not r.skipped]
    err = outcome.first_error
    db.log_event(
        endpoint=endpoint,
        items=len(items),
        cache_hits=sum(1 for r in items if r.cached),
        glossary_direct=sum(1 for r in items if r.direct),
        llm_calls=outcome.llm_calls,
        chars_in=sum(len(r.source) for r in items),
        chars_cached=sum(len(r.source) for r in items if r.cached or r.direct),
        # Compliance only counts mandatory terms: a conditional term left out may be correct.
        terms_matched=sum(sum(1 for t in item_terms[i] if not t.conditional)
                          for i, r in enumerate(outcome.items) if not r.skipped and not r.error),
        terms_applied=sum(1 for r in items for t in r.terms if t["applied"] and not t["conditional"]),
        ms=int((time.perf_counter() - started) * 1000),
        ok=err is None,
        error=err.raw if err else "",
        error_code=err.code if err else "",
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


def parse_paragraph_prompt(text: str) -> list[tuple[int, str]] | None:
    """Immersive Translate sends several paragraphs per request:

        Translate to Simplified Chinese Language:

        [[p0]]
         Student News
        [[p1]]
         Events
        [[source_end]]

    Returns [(0, "Student News"), (1, "Events")], or None for any other format."""
    if "[[p0]]" not in text:
        return None
    end = text.find("[[source_end]]")
    body = text[:end] if end != -1 else text
    parts = _P_MARK.split(body)
    if len(parts) < 3:
        return None
    items = [(int(parts[i]), parts[i + 1].strip()) for i in range(1, len(parts), 2)]
    return items or None


@dataclass
class ProxyResult:
    content: str
    model: str
    cache: str  # "hit", "partial" or "miss"


async def proxy_chat(db: Database, body: dict, set_ids: list[int] | None = None) -> ProxyResult:
    settings = load_settings(db)
    messages = body.get("messages")
    if not isinstance(messages, list) or not messages:
        raise LLMError("Request must include a non-empty 'messages' array.", 400, "bad_request")
    messages = [m for m in messages if isinstance(m, dict)]
    all_text = "\n".join(_content_text(m.get("content")) for m in messages)
    user_msgs = [_content_text(m.get("content")) for m in messages if m.get("role") == "user"]
    target = detect_target_from_prompt(all_text) or settings.default_target_lang

    paragraphs = parse_paragraph_prompt(user_msgs[-1]) if user_msgs else None
    if paragraphs:
        outcome = await translate_texts(db, [t for _, t in paragraphs], target, "openai", set_ids)
        if outcome.first_error:
            raise outcome.first_error
        lines = []
        for (idx, _), item in zip(paragraphs, outcome.items):
            lines.append(f"[[p{idx}]]\n{item.text}")
        served = [i for i in outcome.items if not i.skipped]
        from_memory = sum(1 for i in served if i.cached or i.direct)
        cache = "hit" if served and from_memory == len(served) else ("partial" if from_memory else "miss")
        return ProxyResult("\n".join(lines) + "\n[[source_end]]", settings.model, cache)
    return await _proxy_whole(db, settings, messages, user_msgs, target, set_ids)


async def _proxy_whole(db: Database, settings: EngineSettings, messages: list[dict],
                       user_msgs: list[str], target: str, set_ids: list[int] | None) -> ProxyResult:
    """Fallback for prompts that are not in the paragraph format: forward the whole request,
    adding the glossary to the system prompt, and cache the complete answer."""
    started = time.perf_counter()
    user_text = "\n".join(user_msgs)
    terms = MATCHER.view(db, target, default_sets(db) if set_ids is None else set_ids).match(user_text)
    extra = []
    if settings.style_guide.strip():
        extra.append("Team style guide:\n" + settings.style_guide.strip())
    if terms:
        extra.append(glossary_block(terms))
    new_messages = [dict(m) for m in messages]
    if extra:
        addition = "\n\n".join(extra)
        if new_messages[0].get("role") == "system" and isinstance(new_messages[0].get("content"), str):
            new_messages[0]["content"] = new_messages[0]["content"].rstrip() + "\n\n" + addition
        else:
            new_messages.insert(0, {"role": "system", "content": addition})

    key = sha("c", PROMPT_VERSION, settings.model, json.dumps(new_messages, ensure_ascii=False, sort_keys=True))
    hit = db.cache_get_many([key])
    cached = key in hit
    err: LLMError | None = None
    content = ""
    try:
        if cached:
            content = hit[key]
        else:
            content = await call_once(db, new_messages)
            db.cache_put_many([(key, "chat", user_text[:4000], content, target, settings.model)])
    except LLMError as exc:
        err = exc
        raise
    finally:
        db.log_event(
            endpoint="openai",
            items=1,
            cache_hits=int(cached),
            llm_calls=0 if cached or err else 1,
            chars_in=len(user_text),
            chars_cached=len(user_text) if cached else 0,
            terms_matched=sum(1 for t in terms if not t.conditional) if not err else 0,
            terms_applied=sum(1 for t in terms if not t.conditional and term_applied(t, content)) if not err else 0,
            ms=int((time.perf_counter() - started) * 1000),
            ok=err is None,
            error=err.raw if err else "",
            error_code=err.code if err else "",
        )
    db.add_term_hits(t.id for t in terms)
    return ProxyResult(content, settings.model, "hit" if cached else "miss")


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
                f"{lang_name(target)} translation and a short note written in {lang_name(target)}. "
                "Reply with ONLY a JSON array of objects "
                'like {"source": "...", "target": "...", "note": "..."}.'
            ),
        },
        {"role": "user", "content": text[:12000]},
    ]
    raw = await call_once(db, messages)
    raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw.strip())
    start, end = raw.find("["), raw.rfind("]")
    if start == -1 or end <= start:
        raise LLMError("The AI did not return a list of terms. Try again with a shorter text.", 502,
                       "suggest_failed", raw=raw[:2000])
    try:
        data = json.loads(raw[start : end + 1])
    except json.JSONDecodeError as exc:
        raise LLMError("The AI returned malformed JSON. Try again.", 502, "suggest_failed", raw=raw[:2000]) from exc
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
