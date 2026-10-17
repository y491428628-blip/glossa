"""HTTP routes. Run with: uvicorn app.main:create_app --factory"""

from __future__ import annotations

import csv
import io
import json
import os
import time
import uuid
from pathlib import Path

from fastapi import Body, Depends, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from . import __version__
from .auth import (
    LIMITER,
    check_password,
    ensure_secrets,
    hash_password,
    new_team_key,
    require_admin,
    require_team_key,
    setup_required,
)
from .db import Database, get_db
from .engine import (
    PRESETS,
    LLMError,
    chat_completion,
    detected_lang,
    load_settings,
    proxy_chat,
    suggest_terms,
    translate_texts,
)
from .languages import LANG_NAMES, canon

STATIC_DIR = Path(__file__).parent / "static"
SETTING_KEYS = {
    "llm_base_url", "llm_api_key", "llm_model", "temperature", "default_target_lang",
    "style_guide", "price_per_m_tokens", "team_name", "provider",
}


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

    @app.exception_handler(LLMError)
    async def llm_error_handler(_: Request, exc: LLMError):
        return JSONResponse(
            status_code=exc.status,
            content={"error": {"message": exc.message, "type": "glossa_error"}, "detail": exc.message},
        )

    # ------------------------------------------------------------------ pages
    @app.get("/", include_in_schema=False)
    async def index():
        return FileResponse(STATIC_DIR / "index.html")

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    @app.get("/healthz", include_in_schema=False)
    async def healthz():
        get_db().query_one("SELECT 1 AS ok")
        return {"ok": True, "version": __version__}

    # ------------------------------------------------------------------ translation endpoints
    @app.post("/imme")
    @app.post("/translate")
    async def immersive_custom_api(request: Request, db: Database = Depends(db_dep)):
        """Immersive Translate 'custom API' format:
        {source_lang, target_lang, text_list} -> {translations: [{detected_source_lang, text}]}"""
        require_team_key(request, db)
        try:
            body = await request.json()
        except json.JSONDecodeError:
            raise HTTPException(400, "Body must be JSON.")
        text_list = body.get("text_list")
        if isinstance(text_list, str):
            text_list = [text_list]
        if not isinstance(text_list, list) or not all(isinstance(t, str) for t in text_list):
            raise HTTPException(400, "'text_list' must be an array of strings.")
        if len(text_list) > 500:
            raise HTTPException(413, "Too many paragraphs in one request (max 500).")
        outcome = await translate_texts(db, text_list, body.get("target_lang"), "immersive")
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
        try:
            body = await request.json()
        except json.JSONDecodeError:
            raise HTTPException(400, "Body must be JSON.")
        content, model, cached = await proxy_chat(db, body)
        cid = "chatcmpl-glossa-" + uuid.uuid4().hex[:20]
        created = int(time.time())
        headers = {"X-Glossa-Cache": "hit" if cached else "miss"}
        if body.get("stream"):
            def sse():
                first = {
                    "id": cid, "object": "chat.completion.chunk", "created": created, "model": model,
                    "choices": [{"index": 0, "delta": {"role": "assistant", "content": content}, "finish_reason": None}],
                }
                last = {
                    "id": cid, "object": "chat.completion.chunk", "created": created, "model": model,
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
                "model": model,
                "choices": [
                    {"index": 0, "message": {"role": "assistant", "content": content}, "finish_reason": "stop"}
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
            raise HTTPException(409, "Glossa is already set up.")
        password = str(payload.get("password", ""))
        if len(password) < 8:
            raise HTTPException(400, "Use at least 8 characters.")
        db.set_setting("admin_password_hash", hash_password(password))
        if payload.get("team_name"):
            db.set_setting("team_name", str(payload["team_name"])[:80])
        request.session["admin"] = True
        return {"ok": True}

    @app.post("/api/login")
    async def login(request: Request, payload: dict = Body(...), db: Database = Depends(db_dep)):
        cid = client_id(request)
        if LIMITER.blocked(cid):
            raise HTTPException(429, "Too many attempts. Wait a few minutes and try again.")
        if not check_password(db, str(payload.get("password", ""))):
            LIMITER.fail(cid)
            raise HTTPException(401, "Wrong password.")
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
            "configured": s.configured,
            "model": s.model,
            "default_target_lang": s.default_target_lang,
            "stats": compute_stats(db, s.price_per_m_tokens),
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
                    raise HTTPException(400, "Temperature must be a number.")
                if not 0 <= t <= 2:
                    raise HTTPException(400, "Temperature must be between 0 and 2.")
            if key == "price_per_m_tokens" and value:
                try:
                    float(value)
                except ValueError:
                    raise HTTPException(400, "Price must be a number.")
            db.set_setting(key, value[:20000])
        return await get_settings(db)

    @app.post("/api/settings/test", dependencies=[Depends(admin_dep)])
    async def test_settings(db: Database = Depends(db_dep)):
        s = load_settings(db)
        started = time.perf_counter()
        reply = await chat_completion(
            s, [{"role": "user", "content": "Translate to Simplified Chinese, reply with the translation only: Good morning"}]
        )
        return {"ok": True, "reply": reply[:200], "ms": int((time.perf_counter() - started) * 1000), "model": s.model}

    # ------------------------------------------------------------------ glossary
    def clean_term(payload: dict, db: Database) -> dict:
        source = str(payload.get("source", "")).strip()
        target = str(payload.get("target", "")).strip()
        if not source or not target:
            raise HTTPException(400, "Both the term and its translation are required.")
        if len(source) > 200 or len(target) > 200:
            raise HTTPException(400, "Terms are limited to 200 characters.")
        lang = canon(payload.get("target_lang") or load_settings(db).default_target_lang)
        return {
            "source": source,
            "target": target,
            "target_lang": lang,
            "note": str(payload.get("note", "")).strip()[:300],
            "case_sensitive": bool(payload.get("case_sensitive", False)),
        }

    @app.get("/api/glossary", dependencies=[Depends(admin_dep)])
    async def list_glossary(q: str = "", db: Database = Depends(db_dep)):
        return {"terms": db.list_glossary(q.strip())}

    @app.post("/api/glossary", dependencies=[Depends(admin_dep)])
    async def add_term(payload: dict = Body(...), db: Database = Depends(db_dep)):
        t = clean_term(payload, db)
        return db.upsert_term(**t)

    @app.post("/api/glossary/bulk", dependencies=[Depends(admin_dep)])
    async def add_terms(payload: dict = Body(...), db: Database = Depends(db_dep)):
        added = 0
        for item in payload.get("terms", [])[:1000]:
            try:
                db.upsert_term(**clean_term(item, db))
                added += 1
            except HTTPException:
                continue
        return {"added": added}

    @app.put("/api/glossary/{term_id}", dependencies=[Depends(admin_dep)])
    async def edit_term(term_id: int, payload: dict = Body(...), db: Database = Depends(db_dep)):
        t = clean_term(payload, db)
        clash = db.query_one(
            "SELECT id FROM glossary WHERE source = ? AND target_lang = ? AND id != ?",
            (t["source"], t["target_lang"], term_id),
        )
        if clash:
            raise HTTPException(409, "That term already exists for this language.")
        row = db.update_term(term_id, **t)
        if not row:
            raise HTTPException(404, "Term not found.")
        return row

    @app.delete("/api/glossary/{term_id}", dependencies=[Depends(admin_dep)])
    async def remove_term(term_id: int, db: Database = Depends(db_dep)):
        if not db.delete_term(term_id):
            raise HTTPException(404, "Term not found.")
        return {"ok": True}

    @app.get("/api/glossary/export", dependencies=[Depends(admin_dep)])
    async def export_glossary(db: Database = Depends(db_dep)):
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["source", "target", "target_lang", "note", "case_sensitive"])
        for r in db.list_glossary():
            w.writerow([r["source"], r["target"], r["target_lang"], r["note"], r["case_sensitive"]])
        return Response(
            "﻿" + buf.getvalue(),
            media_type="text/csv; charset=utf-8",
            headers={"Content-Disposition": 'attachment; filename="glossa-glossary.csv"'},
        )

    @app.post("/api/glossary/import", dependencies=[Depends(admin_dep)])
    async def import_glossary(payload: dict = Body(...), db: Database = Depends(db_dep)):
        text = str(payload.get("csv", "")).lstrip("﻿")
        if not text.strip():
            raise HTTPException(400, "The file is empty.")
        rows = list(csv.reader(io.StringIO(text)))
        if rows and [c.strip().lower() for c in rows[0][:2]] == ["source", "target"]:
            header = [c.strip().lower() for c in rows[0]]
            rows = rows[1:]
        else:
            header = ["source", "target", "target_lang", "note", "case_sensitive"]
        added, skipped = 0, 0
        for row in rows[:5000]:
            item = {header[i]: row[i] for i in range(min(len(header), len(row)))}
            item["case_sensitive"] = str(item.get("case_sensitive", "")).strip().lower() in ("1", "true", "yes")
            try:
                db.upsert_term(**clean_term(item, db))
                added += 1
            except HTTPException:
                skipped += 1
        return {"added": added, "skipped": skipped}

    @app.post("/api/glossary/suggest", dependencies=[Depends(admin_dep)])
    async def suggest(payload: dict = Body(...), db: Database = Depends(db_dep)):
        text = str(payload.get("text", "")).strip()
        if len(text) < 20:
            raise HTTPException(400, "Paste a longer sample (a paragraph or more).")
        return {"terms": await suggest_terms(db, text, payload.get("target_lang"))}

    # ------------------------------------------------------------------ try it / cache / stats
    @app.post("/api/translate", dependencies=[Depends(admin_dep)])
    async def try_translate(payload: dict = Body(...), db: Database = Depends(db_dep)):
        text = str(payload.get("text", ""))
        if not text.strip():
            raise HTTPException(400, "Type or paste something to translate.")
        if len(text) > 20000:
            raise HTTPException(413, "Keep the sample under 20,000 characters.")
        paragraphs = text.split("\n")
        started = time.perf_counter()
        outcome = await translate_texts(db, paragraphs, payload.get("target_lang"), "dashboard")
        return {
            "ms": int((time.perf_counter() - started) * 1000),
            "llm_calls": outcome.llm_calls,
            "items": [
                {"source": i.source, "text": i.text, "cached": i.cached, "skipped": i.skipped, "terms": i.terms}
                for i in outcome.items
            ],
        }

    @app.post("/api/cache/clear", dependencies=[Depends(admin_dep)])
    async def clear_cache(db: Database = Depends(db_dep)):
        return {"cleared": db.cache_clear()}

    @app.get("/api/stats", dependencies=[Depends(admin_dep)])
    async def stats(db: Database = Depends(db_dep)):
        return compute_stats(db, load_settings(db).price_per_m_tokens)

    @app.get("/robots.txt", include_in_schema=False)
    async def robots():
        return PlainTextResponse("User-agent: *\nDisallow: /\n")

    return app


def compute_stats(db: Database, price_per_m: float) -> dict:
    tot = db.query_one(
        "SELECT COUNT(*) AS requests, COALESCE(SUM(items),0) AS items, COALESCE(SUM(cache_hits),0) AS cache_hits, "
        "COALESCE(SUM(llm_calls),0) AS llm_calls, COALESCE(SUM(chars_in),0) AS chars_in, "
        "COALESCE(SUM(chars_cached),0) AS chars_cached, COALESCE(SUM(terms_matched),0) AS terms_matched, "
        "COALESCE(SUM(terms_applied),0) AS terms_applied, COALESCE(SUM(1 - ok),0) AS errors FROM events"
    ) or {}
    items = tot.get("items", 0) or 0
    hits = tot.get("cache_hits", 0) or 0
    matched = tot.get("terms_matched", 0) or 0
    # Rough token estimate: ~4 characters per token for the input, and a similar amount of output.
    tokens_saved = int((tot.get("chars_cached", 0) or 0) / 4 * 2)
    since = time.time() - 13 * 86400
    day_rows = db.query(
        "SELECT date(ts, 'unixepoch', 'localtime') AS day, SUM(items) AS items, SUM(cache_hits) AS hits "
        "FROM events WHERE ts >= ? GROUP BY day ORDER BY day",
        (since - (since % 86400),),
    )
    by_day = {r["day"]: r for r in day_rows}
    days = []
    for n in range(13, -1, -1):
        d = time.strftime("%Y-%m-%d", time.localtime(time.time() - n * 86400))
        r = by_day.get(d, {})
        days.append({"day": d, "items": r.get("items", 0) or 0, "hits": r.get("hits", 0) or 0})
    recent = db.query(
        "SELECT ts, endpoint, items, cache_hits, llm_calls, terms_matched, ms, ok, error "
        "FROM events ORDER BY id DESC LIMIT 12"
    )
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
        "hit_rate": round(hits / items, 4) if items else 0,
        "llm_calls": tot.get("llm_calls", 0),
        "errors": tot.get("errors", 0),
        "terms_matched": matched,
        "terms_applied": tot.get("terms_applied", 0),
        "glossary_compliance": round((tot.get("terms_applied", 0) or 0) / matched, 4) if matched else None,
        "tokens_saved_est": tokens_saved,
        "usd_saved_est": round(tokens_saved * price_per_m / 1_000_000, 4),
        "glossary_count": glossary_count,
        "cache_entries": cache_count,
        "days": days,
        "recent": recent,
        "top_terms": top_terms,
    }
