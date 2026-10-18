"""HTTP routes. Run with: uvicorn app.main:create_app --factory"""

from __future__ import annotations

import csv
import io
import json
import os
import time
import uuid
from pathlib import Path

from fastapi import Body, Depends, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from . import __version__
from .auth import (
    LIMITER,
    LoginLimiter,
    check_password,
    ensure_secrets,
    hash_password,
    new_team_key,
    require_admin,
    require_team_key,
    setup_required,
)
from .db import Database, HistoryConflict, get_db
from .engine import (
    PRESETS,
    call_once,
    detected_lang,
    load_settings,
    proxy_chat,
    static_warnings,
    suggest_terms,
    term_warnings,
    translate_texts,
)
from .errors import AppError, LLMError
from .languages import LANG_NAMES, canon

STATIC_DIR = Path(__file__).parent / "static"
SETTING_KEYS = {
    "llm_base_url", "llm_api_key", "llm_model", "temperature", "default_target_lang",
    "style_guide", "price_per_m_tokens", "team_name", "provider", "max_rpm",
}
MAX_PENDING_PROPOSALS = 300
PROPOSAL_LIMITER = LoginLimiter(limit=30, window=3600.0)  # per teammate's address, per hour


def new_batch(kind: str) -> str:
    return f"{kind}-{uuid.uuid4().hex[:12]}"


def db_dep() -> Database:
    return get_db()


def admin_dep(request: Request) -> None:
    require_admin(request)


def public_base(request: Request) -> str:
    env = os.environ.get("PUBLIC_URL", "").strip().rstrip("/")
    if env:
        return env
    proto = request.headers.get("x-forwarded-proto", request.url.scheme)
    host = request.headers.get("x-forwarded-host", request.headers.get("host", request.url.netloc))
    return f"{proto}://{host}"


def mask(key: str) -> str:
    if not key:
        return ""
    return key[:3] + "…" + key[-4:] if len(key) > 10 else "•" * len(key)


def error_body(code: str, message: str, params: dict | None = None, raw: str = "") -> dict:
    """OpenAI-style `error` for API clients, plus `code`/`params` for the dashboard's translations."""
    body = {
        "error": {"message": message, "type": "glossa_error", "code": code},
        "detail": message,
        "code": code,
        "params": params or {},
    }
    if raw and raw != message:
        body["raw"] = raw[:4000]
    return body


def create_app() -> FastAPI:
    db = get_db()
    ensure_secrets(db)

    app = FastAPI(title="Glossa", version=__version__, docs_url=None, redoc_url=None)
    app.add_middleware(
        SessionMiddleware,
        secret_key=db.get_setting("session_secret"),
        session_cookie="glossa_session",
        max_age=60 * 60 * 24 * 14,
        same_site="lax",
    )
    # The browser extension calls the translation endpoints with a bearer token, never cookies.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["*"],
        allow_credentials=False,
    )

    @app.exception_handler(AppError)
    async def app_error_handler(_: Request, exc: AppError):
        return JSONResponse(status_code=exc.status, content=error_body(exc.code, exc.message, exc.params))

    @app.exception_handler(LLMError)
    async def llm_error_handler(_: Request, exc: LLMError):
        headers = {}
        if exc.status == 429 and exc.retry_after:
            headers["Retry-After"] = str(max(1, int(round(exc.retry_after))))
        return JSONResponse(status_code=exc.status, content=error_body(exc.code, exc.message, raw=exc.raw),
                            headers=headers)

    @app.exception_handler(RequestValidationError)
    async def validation_handler(_: Request, exc: RequestValidationError):
        return JSONResponse(status_code=400, content=error_body("bad_request", "The request body is not valid."))

    # ------------------------------------------------------------------ pages
    @app.get("/", include_in_schema=False)
    async def index():
        return FileResponse(STATIC_DIR / "index.html")

    @app.get("/suggest", include_in_schema=False)
    async def member_page():
        """Teammates (team key only) can browse the glossary and propose terms here."""
        return FileResponse(STATIC_DIR / "suggest.html")

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    @app.get("/healthz", include_in_schema=False)
    async def healthz():
        get_db().query_one("SELECT 1 AS ok")
        return {"ok": True, "version": __version__}

    async def read_json(request: Request) -> dict:
        try:
            body = await request.json()
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise AppError(400, "bad_request", "Body must be JSON.")
        if not isinstance(body, dict):
            raise AppError(400, "bad_request", "Body must be a JSON object.")
        return body

    # ------------------------------------------------------------------ translation endpoints
    @app.post("/imme")
    @app.post("/translate")
    async def immersive_custom_api(request: Request, db: Database = Depends(db_dep)):
        """Immersive Translate 'custom API' format:
        {source_lang, target_lang, text_list} -> {translations: [{detected_source_lang, text}]}"""
        require_team_key(request, db)
        body = await read_json(request)
        text_list = body.get("text_list")
        if isinstance(text_list, str):
            text_list = [text_list]
        if not isinstance(text_list, list) or not all(isinstance(t, str) for t in text_list):
            raise AppError(400, "bad_request", "'text_list' must be an array of strings.")
        if len(text_list) > 500:
            raise AppError(413, "too_many_paragraphs", "Too many paragraphs in one request (max 500).")
        outcome = await translate_texts(db, text_list, body.get("target_lang"), "immersive")
        if outcome.first_error:
            raise outcome.first_error  # finished paragraphs are already in memory for the retry
        src = body.get("source_lang")
        return {
            "translations": [
                {"detected_source_lang": detected_lang(src, item.source), "text": item.text}
                for item in outcome.items
            ]
        }

    @app.get("/v1/models")
    async def list_models(request: Request, db: Database = Depends(db_dep)):
        require_team_key(request, db)
        model = load_settings(db).model
        return {"object": "list", "data": [{"id": model, "object": "model", "owned_by": "glossa"}]}

    @app.post("/v1/chat/completions")
    async def openai_proxy(request: Request, db: Database = Depends(db_dep)):
        require_team_key(request, db)
        body = await read_json(request)
        result = await proxy_chat(db, body)
        cid = "chatcmpl-glossa-" + uuid.uuid4().hex[:20]
        created = int(time.time())
        headers = {"X-Glossa-Cache": result.cache}
        if body.get("stream"):
            def sse():
                first = {
                    "id": cid, "object": "chat.completion.chunk", "created": created, "model": result.model,
                    "choices": [{"index": 0, "delta": {"role": "assistant", "content": result.content},
                                 "finish_reason": None}],
                }
                last = {
                    "id": cid, "object": "chat.completion.chunk", "created": created, "model": result.model,
                    "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                }
                yield f"data: {json.dumps(first, ensure_ascii=False)}\n\n"
                yield f"data: {json.dumps(last)}\n\n"
                yield "data: [DONE]\n\n"

            return StreamingResponse(sse(), media_type="text/event-stream", headers=headers)
        return JSONResponse(
            {
                "id": cid,
                "object": "chat.completion",
                "created": created,
                "model": result.model,
                "choices": [
                    {"index": 0, "message": {"role": "assistant", "content": result.content}, "finish_reason": "stop"}
                ],
                "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
            },
            headers=headers,
        )

    # ------------------------------------------------------------------ session
    @app.get("/api/me")
    async def me(request: Request, db: Database = Depends(db_dep)):
        return {
            "authenticated": bool(request.session.get("admin")),
            "setup_required": setup_required(db),
            "team_name": db.get_setting("team_name", "") or "",
            "version": __version__,
        }

    def client_id(request: Request) -> str:
        fwd = request.headers.get("x-forwarded-for", "")
        return fwd.split(",")[0].strip() or (request.client.host if request.client else "?")

    @app.post("/api/setup")
    async def setup(request: Request, payload: dict = Body(...), db: Database = Depends(db_dep)):
        if not setup_required(db):
            raise AppError(409, "already_set_up", "Glossa is already set up.")
        password = str(payload.get("password", ""))
        if len(password) < 8:
            raise AppError(400, "password_too_short", "Use at least 8 characters.", min=8)
        db.set_setting("admin_password_hash", hash_password(password))
        if payload.get("team_name"):
            db.set_setting("team_name", str(payload["team_name"])[:80])
        request.session["admin"] = True
        return {"ok": True}

    @app.post("/api/login")
    async def login(request: Request, payload: dict = Body(...), db: Database = Depends(db_dep)):
        cid = client_id(request)
        if LIMITER.blocked(cid):
            raise AppError(429, "too_many_attempts", "Too many attempts. Wait a few minutes and try again.")
        if not check_password(db, str(payload.get("password", ""))):
            LIMITER.fail(cid)
            raise AppError(401, "wrong_password", "Wrong password.")
        LIMITER.reset(cid)
        request.session["admin"] = True
        return {"ok": True}

    @app.post("/api/logout")
    async def logout(request: Request):
        request.session.clear()
        return {"ok": True}

    # ------------------------------------------------------------------ dashboard data
    @app.get("/api/overview", dependencies=[Depends(admin_dep)])
    async def overview(request: Request, db: Database = Depends(db_dep)):
        base = public_base(request)
        team_key = db.get_setting("team_key", "")
        s = load_settings(db)
        return {
            "base_url": base,
            "team_key": team_key,
            "endpoints": {
                "openai": f"{base}/v1/chat/completions",
                "custom": f"{base}/imme?token={team_key}",
            },
            "suggest_url": f"{base}/suggest#key={team_key}",
            "pending_proposals": db.count_pending_proposals(),
            "configured": s.configured,
            "model": s.model,
            "default_target_lang": s.default_target_lang,
            "stats": compute_stats(db, s.price_per_m_tokens, s.max_rpm),
        }

    @app.post("/api/team-key/rotate", dependencies=[Depends(admin_dep)])
    async def rotate_team_key(db: Database = Depends(db_dep)):
        key = new_team_key()
        db.set_setting("team_key", key)
        return {"team_key": key}

    @app.get("/api/settings", dependencies=[Depends(admin_dep)])
    async def get_settings(db: Database = Depends(db_dep)):
        s = load_settings(db)
        return {
            "provider": db.get_setting("provider", "") or "",
            "llm_base_url": s.base_url,
            "llm_api_key_masked": mask(s.api_key),
            "has_api_key": bool(s.api_key),
            "llm_model": s.model,
            "temperature": "" if s.temperature is None else s.temperature,
            "default_target_lang": s.default_target_lang,
            "style_guide": s.style_guide,
            "price_per_m_tokens": s.price_per_m_tokens,
            "max_rpm": s.max_rpm,
            "team_name": db.get_setting("team_name", "") or "",
            "presets": PRESETS,
            "languages": LANG_NAMES,
        }

    @app.put("/api/settings", dependencies=[Depends(admin_dep)])
    async def put_settings(payload: dict = Body(...), db: Database = Depends(db_dep)):
        for key, value in payload.items():
            if key not in SETTING_KEYS:
                continue
            value = "" if value is None else str(value).strip()
            if key == "llm_api_key" and not value:
                continue  # empty means "keep the current key"
            if key == "default_target_lang":
                value = canon(value)
            if key == "temperature" and value:
                try:
                    t = float(value)
                except ValueError:
                    raise AppError(400, "temperature_invalid", "Temperature must be a number between 0 and 2.")
                if not 0 <= t <= 2:
                    raise AppError(400, "temperature_invalid", "Temperature must be a number between 0 and 2.")
            if key == "price_per_m_tokens" and value:
                try:
                    if float(value) < 0:
                        raise ValueError
                except ValueError:
                    raise AppError(400, "price_invalid", "Price must be a number of 0 or more.")
            if key == "max_rpm" and value:
                try:
                    n = int(float(value))
                except ValueError:
                    raise AppError(400, "rpm_invalid", "Requests per minute must be a whole number of 0 or more.")
                if n < 0:
                    raise AppError(400, "rpm_invalid", "Requests per minute must be a whole number of 0 or more.")
                value = str(n)
            db.set_setting(key, value[:20000])
        return await get_settings(db)

    @app.post("/api/settings/test", dependencies=[Depends(admin_dep)])
    async def test_settings(db: Database = Depends(db_dep)):
        s = load_settings(db)
        started = time.perf_counter()
        reply = await call_once(
            db, [{"role": "user", "content": "Translate to Simplified Chinese, reply with the translation only: Good morning"}]
        )
        return {"ok": True, "reply": reply[:200], "ms": int((time.perf_counter() - started) * 1000), "model": s.model}

    # ------------------------------------------------------------------ glossary
    def clean_term(payload: dict, db: Database) -> dict:
        source = str(payload.get("source", "")).strip()
        target = str(payload.get("target", "")).strip()
        if not source or not target:
            raise AppError(400, "term_required", "Both the term and its translation are required.")
        if len(source) > 200 or len(target) > 200:
            raise AppError(400, "term_too_long", "Terms are limited to 200 characters.", max=200)
        lang = canon(payload.get("target_lang") or load_settings(db).default_target_lang)
        note = str(payload.get("note", "")).strip()[:300]
        conditional = truthy(payload.get("conditional", False))
        if conditional and not note:
            raise AppError(400, "condition_needs_note", "Say in the note when this translation applies.")
        return {
            "source": source,
            "target": target,
            "target_lang": lang,
            "note": note,
            "case_sensitive": truthy(payload.get("case_sensitive", False)),
            "conditional": conditional,
        }

    def truthy(value) -> bool:
        if isinstance(value, str):
            return value.strip().lower() in ("1", "true", "yes")
        return bool(value)

    def check_warnings(t: dict, payload: dict, db: Database) -> None:
        """Very short, everyday or very broad terms are saved only after the admin confirms."""
        if truthy(payload.get("force")):
            return
        report = term_warnings(db, t["source"], t["target_lang"], t["case_sensitive"])
        if report["warnings"]:
            raise AppError(409, "term_warning", f"Check “{t['source']}” before saving it.", term=t["source"], **report)

    @app.get("/api/glossary", dependencies=[Depends(admin_dep)])
    async def list_glossary(q: str = "", db: Database = Depends(db_dep)):
        terms = db.list_glossary(q.strip())
        for row in terms:
            row["warn"] = static_warnings(row["source"], bool(row["case_sensitive"]))
        return {"terms": terms}

    @app.post("/api/glossary", dependencies=[Depends(admin_dep)])
    async def add_term(payload: dict = Body(...), db: Database = Depends(db_dep)):
        t = clean_term(payload, db)
        existing = db.find_term(t["source"], t["target_lang"])
        if not existing or bool(existing["case_sensitive"]) != t["case_sensitive"]:
            check_warnings(t, payload, db)
        return db.upsert_term(**t)

    @app.post("/api/glossary/bulk", dependencies=[Depends(admin_dep)])
    async def add_terms(payload: dict = Body(...), db: Database = Depends(db_dep)):
        """Used for AI suggestions and the example terms. One history group, so one undo."""
        added = 0
        batch = new_batch("bulk")
        for item in payload.get("terms", [])[:1000]:
            try:
                db.upsert_term(**clean_term(item, db), batch=batch)
                added += 1
            except AppError:
                continue
        return {"added": added}

    @app.put("/api/glossary/{term_id}", dependencies=[Depends(admin_dep)])
    async def edit_term(term_id: int, payload: dict = Body(...), db: Database = Depends(db_dep)):
        t = clean_term(payload, db)
        current = db.get_term(term_id)
        if not current:
            raise AppError(404, "term_not_found", "Term not found.")
        clash = db.query_one(
            "SELECT id FROM glossary WHERE source = ? AND target_lang = ? AND id != ?",
            (t["source"], t["target_lang"], term_id),
        )
        if clash:
            raise AppError(409, "term_exists", "That term already exists for this language.")
        if (current["source"], current["target_lang"], bool(current["case_sensitive"])) != (
            t["source"], t["target_lang"], t["case_sensitive"]
        ):
            check_warnings(t, payload, db)  # only when what the term matches changes
        row = db.update_term(term_id, **t)
        if not row:
            raise AppError(404, "term_not_found", "Term not found.")
        return row

    @app.delete("/api/glossary/{term_id}", dependencies=[Depends(admin_dep)])
    async def remove_term(term_id: int, db: Database = Depends(db_dep)):
        if not db.delete_term(term_id):
            raise AppError(404, "term_not_found", "Term not found.")
        return {"ok": True}

    @app.get("/api/glossary/export", dependencies=[Depends(admin_dep)])
    async def export_glossary(db: Database = Depends(db_dep)):
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["source", "target", "target_lang", "note", "case_sensitive", "conditional"])
        for r in db.list_glossary():
            w.writerow([r["source"], r["target"], r["target_lang"], r["note"], r["case_sensitive"], r["conditional"]])
        return Response(
            "﻿" + buf.getvalue(),
            media_type="text/csv; charset=utf-8",
            headers={"Content-Disposition": 'attachment; filename="glossa-glossary.csv"'},
        )

    @app.post("/api/glossary/import", dependencies=[Depends(admin_dep)])
    async def import_glossary(payload: dict = Body(...), db: Database = Depends(db_dep)):
        text = str(payload.get("csv", "")).lstrip("﻿")
        if not text.strip():
            raise AppError(400, "file_empty", "The file is empty.")
        rows = list(csv.reader(io.StringIO(text)))
        if rows and [c.strip().lower() for c in rows[0][:2]] == ["source", "target"]:
            header = [c.strip().lower() for c in rows[0]]
            rows = rows[1:]
        else:
            header = ["source", "target", "target_lang", "note", "case_sensitive", "conditional"]
        added, skipped = 0, 0
        batch = new_batch("import")  # the whole file is one history group, so one undo
        for row in rows[:5000]:
            item = {header[i]: row[i] for i in range(min(len(header), len(row)))}
            try:
                db.upsert_term(**clean_term(item, db), batch=batch)
                added += 1
            except AppError:
                skipped += 1
        return {"added": added, "skipped": skipped}

    # ------------------------------------------------------------------ glossary history
    @app.get("/api/glossary/history", dependencies=[Depends(admin_dep)])
    async def glossary_history(db: Database = Depends(db_dep)):
        return {"groups": db.list_history()}

    @app.post("/api/glossary/history/{entry_id}/undo", dependencies=[Depends(admin_dep)])
    async def undo_change(entry_id: int, db: Database = Depends(db_dep)):
        try:
            count = db.undo(entry_id)
        except LookupError:
            raise AppError(404, "history_not_found", "That change is no longer in the history.")
        except HistoryConflict as exc:
            if exc.code == "already_undone":
                raise AppError(409, "already_undone", "That change has already been undone.")
            raise AppError(409, "history_conflict",
                           f"“{exc.term}” has changed since. Undo the newer change first.", term=exc.term)
        return {"undone": count}

    # ------------------------------------------------------------------ proposals from teammates
    def team_term(row: dict) -> dict:
        return {k: row[k] for k in ("source", "target", "target_lang", "note", "case_sensitive", "conditional")}

    @app.get("/api/team/glossary")
    async def team_glossary(request: Request, db: Database = Depends(db_dep)):
        require_team_key(request, db)
        s = load_settings(db)
        return {
            "team_name": db.get_setting("team_name", "") or "",
            "default_target_lang": s.default_target_lang,
            "languages": LANG_NAMES,
            "terms": [team_term(r) for r in db.list_glossary()[:5000]],
        }

    @app.post("/api/team/proposals")
    async def propose_term(request: Request, db: Database = Depends(db_dep)):
        require_team_key(request, db)
        payload = await read_json(request)
        cid = client_id(request)
        if PROPOSAL_LIMITER.blocked(cid):
            raise AppError(429, "too_many_proposals", "Too many suggestions from this device. Try again later.")
        t = clean_term({**payload, "conditional": False, "case_sensitive": False}, db)
        name = str(payload.get("name", "")).strip()
        if len(name) > 40:
            raise AppError(400, "name_too_long", "Keep your name under 40 characters.", max=40)
        if db.count_pending_proposals() >= MAX_PENDING_PROPOSALS:
            raise AppError(429, "proposals_full", "Too many suggestions are waiting for review.")
        PROPOSAL_LIMITER.fail(cid)  # counts towards the hourly limit
        pid = db.add_proposal(t["source"], t["target"], t["target_lang"], t["note"], name, cid)
        return {"ok": True, "id": pid}

    def existing_for(db: Database, source: str, target_lang: str) -> dict | None:
        return db.query_one(
            "SELECT * FROM glossary WHERE lower(source) = lower(?) AND target_lang = ? ORDER BY id LIMIT 1",
            (source, target_lang),
        )

    @app.get("/api/proposals", dependencies=[Depends(admin_dep)])
    async def list_proposals(db: Database = Depends(db_dep)):
        out = []
        for p in db.pending_proposals():
            current = existing_for(db, p["source"], p["target_lang"])
            out.append({
                **p,
                "current": team_term(current) if current else None,
                "warn": static_warnings(p["source"], bool(current and current["case_sensitive"])),
            })
        return {"proposals": out}

    def pending(db: Database, proposal_id: int) -> dict:
        p = db.get_proposal(proposal_id)
        if not p:
            raise AppError(404, "proposal_not_found", "That suggestion no longer exists.")
        if p["status"] != "pending":
            raise AppError(409, "proposal_decided", "That suggestion has already been handled.")
        return p

    @app.post("/api/proposals/{proposal_id}/approve", dependencies=[Depends(admin_dep)])
    async def approve_proposal(proposal_id: int, payload: dict = Body(default={}), db: Database = Depends(db_dep)):
        p = pending(db, proposal_id)
        current = existing_for(db, p["source"], p["target_lang"])
        t = clean_term({
            # An existing term keeps its spelling and settings; the proposal changes the translation.
            "source": current["source"] if current else p["source"],
            "target": payload.get("target") or p["target"],
            "target_lang": p["target_lang"],
            # The teammate's note explains their reason; the admin decides what note the AI should see.
            "note": payload["note"] if "note" in payload else (current["note"] if current else p["note"]),
            "case_sensitive": bool(current and current["case_sensitive"]),
            "conditional": payload.get("conditional", bool(current and current["conditional"])),
        }, db)
        if not current:
            check_warnings(t, payload, db)
        row = db.upsert_term(**t, origin=f"proposal:{p['name']}")
        db.decide_proposal(proposal_id, "approved", row["id"])
        return {"term": row}

    @app.post("/api/proposals/{proposal_id}/reject", dependencies=[Depends(admin_dep)])
    async def reject_proposal(proposal_id: int, db: Database = Depends(db_dep)):
        pending(db, proposal_id)
        db.decide_proposal(proposal_id, "rejected")
        return {"ok": True}

    @app.post("/api/glossary/suggest", dependencies=[Depends(admin_dep)])
    async def suggest(payload: dict = Body(...), db: Database = Depends(db_dep)):
        text = str(payload.get("text", "")).strip()
        if len(text) < 20:
            raise AppError(400, "sample_too_short", "Paste a longer sample (a paragraph or more).")
        return {"terms": await suggest_terms(db, text, payload.get("target_lang"))}

    # ------------------------------------------------------------------ try it / cache / stats
    @app.post("/api/translate", dependencies=[Depends(admin_dep)])
    async def try_translate(payload: dict = Body(...), db: Database = Depends(db_dep)):
        text = str(payload.get("text", ""))
        if not text.strip():
            raise AppError(400, "text_required", "Type or paste something to translate.")
        if len(text) > 20000:
            raise AppError(413, "text_too_long", "Keep the sample under 20,000 characters.", max=20000)
        paragraphs = text.split("\n")
        started = time.perf_counter()
        outcome = await translate_texts(db, paragraphs, payload.get("target_lang"), "dashboard")
        err = outcome.first_error
        if err and all(i.error or i.skipped for i in outcome.items):
            raise err  # nothing came back at all
        return {
            "ms": int((time.perf_counter() - started) * 1000),
            "llm_calls": outcome.llm_calls,
            "error_code": err.code if err else "",
            "error_raw": err.raw[:2000] if err else "",
            "items": [
                {
                    "source": i.source, "text": i.text, "cached": i.cached, "direct": i.direct,
                    "skipped": i.skipped, "terms": i.terms, "error_code": i.error.code if i.error else "",
                }
                for i in outcome.items
            ],
        }

    @app.post("/api/cache/clear", dependencies=[Depends(admin_dep)])
    async def clear_cache(db: Database = Depends(db_dep)):
        return {"cleared": db.cache_clear()}

    @app.get("/api/stats", dependencies=[Depends(admin_dep)])
    async def stats(db: Database = Depends(db_dep)):
        s = load_settings(db)
        return compute_stats(db, s.price_per_m_tokens, s.max_rpm)

    @app.get("/robots.txt", include_in_schema=False)
    async def robots():
        return PlainTextResponse("User-agent: *\nDisallow: /\n")

    return app


def compute_stats(db: Database, price_per_m: float, max_rpm: int) -> dict:
    tot = db.query_one(
        "SELECT COUNT(*) AS requests, COALESCE(SUM(items),0) AS items, COALESCE(SUM(cache_hits),0) AS cache_hits, "
        "COALESCE(SUM(glossary_direct),0) AS glossary_direct, "
        "COALESCE(SUM(chars_in),0) AS chars_in, COALESCE(SUM(chars_cached),0) AS chars_cached, "
        "COALESCE(SUM(terms_matched),0) AS terms_matched, COALESCE(SUM(terms_applied),0) AS terms_applied "
        "FROM events"
    ) or {}
    now = time.time()
    errors_24h = (db.query_one("SELECT COUNT(*) AS n FROM events WHERE ok = 0 AND ts >= ?", (now - 86400,)) or {}).get("n", 0)
    calls = db.query_one(
        "SELECT COUNT(*) AS n, COALESCE(SUM(1 - ok), 0) AS failed FROM upstream_calls WHERE ts >= ?", (now - 3600,)
    ) or {}
    peak = db.query_one(
        "SELECT COALESCE(MAX(n), 0) AS peak FROM (SELECT COUNT(*) AS n FROM upstream_calls WHERE ts >= ? "
        "GROUP BY CAST(ts / 60 AS INTEGER))",
        (now - 3600,),
    ) or {}
    total_calls = (db.query_one("SELECT COUNT(*) AS n FROM upstream_calls WHERE ok = 1") or {}).get("n", 0)
    items = tot.get("items", 0) or 0
    hits = tot.get("cache_hits", 0) or 0
    direct = tot.get("glossary_direct", 0) or 0
    matched = tot.get("terms_matched", 0) or 0
    # Rough token estimate: ~4 characters per token for the input, and a similar amount of output.
    tokens_saved = int((tot.get("chars_cached", 0) or 0) / 4 * 2)
    since = now - 13 * 86400
    day_rows = db.query(
        "SELECT date(ts, 'unixepoch', 'localtime') AS day, SUM(items) AS items, "
        "SUM(cache_hits) + SUM(glossary_direct) AS hits "
        "FROM events WHERE ts >= ? GROUP BY day ORDER BY day",
        (since - (since % 86400),),
    )
    by_day = {r["day"]: r for r in day_rows}
    days = []
    for n in range(13, -1, -1):
        d = time.strftime("%Y-%m-%d", time.localtime(now - n * 86400))
        r = by_day.get(d, {})
        days.append({"day": d, "items": r.get("items", 0) or 0, "hits": r.get("hits", 0) or 0})
    recent = db.query(
        "SELECT ts, endpoint, items, cache_hits, glossary_direct, llm_calls, terms_matched, ms, ok, error, error_code "
        "FROM events ORDER BY id DESC LIMIT 12"
    )
    for r in recent:
        r["error"] = (r.get("error") or "")[:1500]
    top_terms = db.query(
        "SELECT source, target, target_lang, hit_count FROM glossary WHERE hit_count > 0 "
        "ORDER BY hit_count DESC LIMIT 6"
    )
    glossary_count = (db.query_one("SELECT COUNT(*) AS n FROM glossary") or {}).get("n", 0)
    cache_count = (db.query_one("SELECT COUNT(*) AS n FROM cache") or {}).get("n", 0)
    return {
        "requests": tot.get("requests", 0),
        "items": items,
        "cache_hits": hits,
        "glossary_direct": direct,
        "hit_rate": round((hits + direct) / items, 4) if items else 0,
        "llm_calls": total_calls,
        "errors_24h": errors_24h,
        "terms_matched": matched,
        "terms_applied": tot.get("terms_applied", 0),
        "glossary_compliance": round((tot.get("terms_applied", 0) or 0) / matched, 4) if matched else None,
        "tokens_saved_est": tokens_saved,
        "usd_saved_est": round(tokens_saved * price_per_m / 1_000_000, 4),
        "calls_last_hour": calls.get("n", 0),
        "failed_calls_last_hour": calls.get("failed", 0),
        "peak_rpm_last_hour": peak.get("peak", 0),
        "max_rpm": max_rpm,
        "glossary_count": glossary_count,
        "cache_entries": cache_count,
        "days": days,
        "recent": recent,
        "top_terms": top_terms,
    }
