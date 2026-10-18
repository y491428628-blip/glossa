"""SQLite storage: settings, glossary (with change history), team proposals, translation cache and usage events."""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Iterable

DATA_DIR = Path(os.environ.get("DATA_DIR", "/data"))

TERM_FIELDS = ("source", "target", "target_lang", "note", "case_sensitive", "conditional")


def term_state(row: Any) -> dict | None:
    """The user-editable part of a glossary row, used to compare and restore versions."""
    if not row:
        return None
    row = dict(row)
    return {
        "source": row["source"],
        "target": row["target"],
        "target_lang": row["target_lang"],
        "note": row.get("note") or "",
        "case_sensitive": int(bool(row.get("case_sensitive"))),
        "conditional": int(bool(row.get("conditional"))),
    }


def _dump(state: dict | None) -> str:
    return json.dumps(state, ensure_ascii=False, sort_keys=True) if state else ""


def _load(text: str) -> dict | None:
    return json.loads(text) if text else None


class HistoryConflict(Exception):
    """The term changed after this history entry, so undoing it would overwrite newer work."""

    def __init__(self, code: str, term: str = ""):
        super().__init__(code)
        self.code = code
        self.term = term

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS glossary (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    source         TEXT NOT NULL,
    target         TEXT NOT NULL,
    target_lang    TEXT NOT NULL DEFAULT 'zh-CN',
    note           TEXT NOT NULL DEFAULT '',
    case_sensitive INTEGER NOT NULL DEFAULT 0,
    conditional    INTEGER NOT NULL DEFAULT 0,   -- 1: the AI decides from the note whether the term applies
    hit_count      INTEGER NOT NULL DEFAULT 0,
    created_at     REAL NOT NULL,
    updated_at     REAL NOT NULL,
    UNIQUE (source, target_lang)
);

CREATE TABLE IF NOT EXISTS glossary_history (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    ts         REAL NOT NULL,
    action     TEXT NOT NULL,                -- add | edit | delete | undo
    batch      TEXT NOT NULL DEFAULT '',     -- rows changed together: "import-…", "bulk-…", "undo-…"
    term_id    INTEGER NOT NULL DEFAULT 0,
    prev_state TEXT NOT NULL DEFAULT '',     -- JSON of the term before the change; '' if it did not exist
    next_state TEXT NOT NULL DEFAULT '',     -- JSON after the change; '' if it was deleted
    origin     TEXT NOT NULL DEFAULT '',     -- '' for the admin, 'proposal:<name>' for an approved proposal
    undo_of    INTEGER NOT NULL DEFAULT 0,
    undone     INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS proposals (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          REAL NOT NULL,
    source      TEXT NOT NULL,
    target      TEXT NOT NULL,
    target_lang TEXT NOT NULL,
    note        TEXT NOT NULL DEFAULT '',
    name        TEXT NOT NULL DEFAULT '',
    client      TEXT NOT NULL DEFAULT '',
    status      TEXT NOT NULL DEFAULT 'pending',   -- pending | approved | rejected
    decided_at  REAL,
    term_id     INTEGER NOT NULL DEFAULT 0,
    kind        TEXT NOT NULL DEFAULT 'term'       -- term (add or change) | remove
);

CREATE TABLE IF NOT EXISTS cache (
    key         TEXT PRIMARY KEY,
    kind        TEXT NOT NULL,          -- 'para' (single paragraph) or 'chat' (whole proxied request)
    source_text TEXT NOT NULL,
    translated  TEXT NOT NULL,
    target_lang TEXT NOT NULL,
    model       TEXT NOT NULL,
    hits        INTEGER NOT NULL DEFAULT 0,
    created_at  REAL NOT NULL,
    last_hit_at REAL
);

CREATE TABLE IF NOT EXISTS events (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    ts            REAL NOT NULL,
    endpoint      TEXT NOT NULL,
    items         INTEGER NOT NULL DEFAULT 0,
    cache_hits    INTEGER NOT NULL DEFAULT 0,
    llm_calls     INTEGER NOT NULL DEFAULT 0,
    chars_in      INTEGER NOT NULL DEFAULT 0,
    chars_cached  INTEGER NOT NULL DEFAULT 0,
    terms_matched INTEGER NOT NULL DEFAULT 0,
    terms_applied INTEGER NOT NULL DEFAULT 0,
    ms            INTEGER NOT NULL DEFAULT 0,
    ok            INTEGER NOT NULL DEFAULT 1,
    error         TEXT NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_events_ts ON events (ts);

CREATE TABLE IF NOT EXISTS upstream_calls (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    ts         REAL NOT NULL,
    model      TEXT NOT NULL,
    items      INTEGER NOT NULL DEFAULT 0,
    status     INTEGER NOT NULL DEFAULT 0,
    ms         INTEGER NOT NULL DEFAULT 0,
    ok         INTEGER NOT NULL DEFAULT 1,
    error_code TEXT NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_upstream_calls_ts ON upstream_calls (ts);
"""

# Columns added after v0.1.0. Existing databases are upgraded in place on startup.
MIGRATIONS = {
    "events": {
        "glossary_direct": "INTEGER NOT NULL DEFAULT 0",
        "error_code": "TEXT NOT NULL DEFAULT ''",
    },
    "glossary": {
        "conditional": "INTEGER NOT NULL DEFAULT 0",
    },
    "proposals": {
        "kind": "TEXT NOT NULL DEFAULT 'term'",
    },
}


class Database:
    def __init__(self, path: Path | str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            self._conn.executescript(SCHEMA)
            for table, columns in MIGRATIONS.items():
                existing = {row[1] for row in self._conn.execute(f"PRAGMA table_info({table})")}
                for name, decl in columns.items():
                    if name not in existing:
                        self._conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")
            self._conn.commit()

    # -- low level -----------------------------------------------------------------
    def execute(self, sql: str, params: Iterable[Any] = ()) -> sqlite3.Cursor:
        with self._lock:
            cur = self._conn.execute(sql, tuple(params))
            self._conn.commit()
            return cur

    def query(self, sql: str, params: Iterable[Any] = ()) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(sql, tuple(params)).fetchall()
        return [dict(r) for r in rows]

    def query_one(self, sql: str, params: Iterable[Any] = ()) -> dict | None:
        rows = self.query(sql, params)
        return rows[0] if rows else None

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # -- settings ------------------------------------------------------------------
    def get_setting(self, key: str, default: str | None = None) -> str | None:
        row = self.query_one("SELECT value FROM settings WHERE key = ?", (key,))
        return row["value"] if row else default

    def set_setting(self, key: str, value: str) -> None:
        self.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )

    def delete_setting(self, key: str) -> None:
        self.execute("DELETE FROM settings WHERE key = ?", (key,))

    # -- glossary ------------------------------------------------------------------
    def bump_glossary_version(self) -> None:
        current = int(self.get_setting("glossary_version", "0") or 0)
        self.set_setting("glossary_version", str(current + 1))

    def glossary_version(self) -> int:
        return int(self.get_setting("glossary_version", "0") or 0)

    def list_glossary(self, q: str = "") -> list[dict]:
        if q:
            like = f"%{q}%"
            return self.query(
                "SELECT * FROM glossary WHERE source LIKE ? OR target LIKE ? OR note LIKE ? "
                "ORDER BY lower(source)",
                (like, like, like),
            )
        return self.query("SELECT * FROM glossary ORDER BY lower(source)")

    def get_term(self, term_id: int) -> dict | None:
        return self.query_one("SELECT * FROM glossary WHERE id = ?", (term_id,))

    def find_term(self, source: str, target_lang: str) -> dict | None:
        return self.query_one("SELECT * FROM glossary WHERE source = ? AND target_lang = ?", (source, target_lang))

    def _record(self, action: str, term_id: int, prev: dict | None, nxt: dict | None,
                batch: str = "", origin: str = "") -> None:
        self.execute(
            "INSERT INTO glossary_history (ts, action, batch, term_id, prev_state, next_state, origin) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (time.time(), action, batch, term_id, _dump(prev), _dump(nxt), origin),
        )

    def upsert_term(
        self,
        source: str,
        target: str,
        target_lang: str,
        note: str = "",
        case_sensitive: bool = False,
        conditional: bool = False,
        batch: str = "",
        origin: str = "",
    ) -> dict:
        prev = term_state(self.find_term(source, target_lang))
        now = time.time()
        self.execute(
            "INSERT INTO glossary (source, target, target_lang, note, case_sensitive, conditional, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(source, target_lang) DO UPDATE SET target = excluded.target, note = excluded.note, "
            "case_sensitive = excluded.case_sensitive, conditional = excluded.conditional, "
            "updated_at = excluded.updated_at",
            (source, target, target_lang, note, int(case_sensitive), int(conditional), now, now),
        )
        row: dict = self.find_term(source, target_lang)  # type: ignore[assignment]
        nxt = term_state(row)
        if nxt != prev:
            self._record("edit" if prev else "add", row["id"], prev, nxt, batch, origin)
            self.bump_glossary_version()
        return row

    def update_term(self, term_id: int, origin: str = "", **fields: Any) -> dict | None:
        prev = term_state(self.get_term(term_id))
        if prev is None:
            return None
        sets = {k: v for k, v in fields.items() if k in TERM_FIELDS and v is not None}
        for flag in ("case_sensitive", "conditional"):
            if flag in sets:
                sets[flag] = int(bool(sets[flag]))
        if sets:
            cols = ", ".join(f"{k} = ?" for k in sets)
            self.execute(f"UPDATE glossary SET {cols}, updated_at = ? WHERE id = ?", (*sets.values(), time.time(), term_id))
        row = self.get_term(term_id)
        nxt = term_state(row)
        if nxt != prev:
            self._record("edit", term_id, prev, nxt, origin=origin)
            self.bump_glossary_version()
        return row

    def delete_term(self, term_id: int, origin: str = "") -> bool:
        prev = term_state(self.get_term(term_id))
        if prev is None:
            return False
        self.execute("DELETE FROM glossary WHERE id = ?", (term_id,))
        self._record("delete", term_id, prev, None, origin=origin)
        self.bump_glossary_version()
        return True

    # -- glossary history ------------------------------------------------------------
    def list_history(self, limit_groups: int = 40, entries_per_group: int = 3) -> list[dict]:
        """Recent changes, newest first. Rows changed together (an import, a bulk add) form one group."""
        rows = self.query("SELECT * FROM glossary_history ORDER BY id DESC LIMIT 2000")
        groups: list[dict] = []
        by_key: dict[str, dict] = {}
        for r in rows:
            entry = {
                "id": r["id"], "ts": r["ts"], "action": r["action"], "origin": r["origin"],
                "undone": bool(r["undone"]), "undo_of": r["undo_of"],
                "prev": _load(r["prev_state"]), "next": _load(r["next_state"]),
            }
            key = r["batch"] or f"#{r['id']}"
            group = by_key.get(key)
            if group is None:
                if len(groups) >= limit_groups:
                    continue
                kind = r["batch"].split("-", 1)[0] if r["batch"] else r["action"]
                group = {"id": r["id"], "ts": r["ts"], "kind": kind, "count": 0, "undone": True, "entries": []}
                by_key[key] = group
                groups.append(group)
            group["count"] += 1
            group["undone"] = group["undone"] and entry["undone"]
            if len(group["entries"]) < entries_per_group:
                group["entries"].append(entry)
        return groups

    def undo(self, entry_id: int) -> int:
        """Reverts a change, or every change made together with it. All or nothing.
        Raises LookupError if the entry does not exist and HistoryConflict if the term changed since."""
        first = self.query_one("SELECT * FROM glossary_history WHERE id = ?", (entry_id,))
        if not first:
            raise LookupError(entry_id)
        group = (
            self.query("SELECT * FROM glossary_history WHERE batch = ? ORDER BY id", (first["batch"],))
            if first["batch"] else [first]
        )
        if any(e["undone"] for e in group):
            raise HistoryConflict("already_undone")
        batch = f"undo-{uuid.uuid4().hex[:12]}" if len(group) > 1 else ""

        def fetch(conn: sqlite3.Connection, state: dict) -> dict | None:
            row = conn.execute("SELECT * FROM glossary WHERE source = ? AND target_lang = ?",
                               (state["source"], state["target_lang"])).fetchone()
            return dict(row) if row else None

        with self._lock:
            conn = self._conn
            try:
                for e in reversed(group):
                    prev, nxt = _load(e["prev_state"]), _load(e["next_state"])
                    shown = (nxt or prev or {}).get("source", "")
                    current = fetch(conn, nxt) if nxt else None
                    if nxt and term_state(current) != nxt:
                        raise HistoryConflict("history_conflict", shown)
                    if prev:
                        occupant = fetch(conn, prev)
                        if occupant and (current is None or occupant["id"] != current["id"]):
                            raise HistoryConflict("history_conflict", shown)
                    now = time.time()
                    if nxt and prev:
                        cols = ", ".join(f"{k} = ?" for k in TERM_FIELDS)
                        conn.execute(f"UPDATE glossary SET {cols}, updated_at = ? WHERE id = ?",
                                     (*[prev[k] for k in TERM_FIELDS], now, current["id"]))  # type: ignore[index]
                        term_id = current["id"]  # type: ignore[index]
                    elif nxt:
                        conn.execute("DELETE FROM glossary WHERE id = ?", (current["id"],))  # type: ignore[index]
                        term_id = current["id"]  # type: ignore[index]
                    else:
                        cur = conn.execute(
                            f"INSERT INTO glossary ({', '.join(TERM_FIELDS)}, created_at, updated_at) "
                            f"VALUES ({', '.join('?' * len(TERM_FIELDS))}, ?, ?)",
                            (*[prev[k] for k in TERM_FIELDS], now, now),  # type: ignore[index]
                        )
                        term_id = cur.lastrowid
                    conn.execute(
                        "INSERT INTO glossary_history (ts, action, batch, term_id, prev_state, next_state, undo_of) "
                        "VALUES (?, 'undo', ?, ?, ?, ?, ?)",
                        (now, batch, term_id, _dump(nxt), _dump(prev), e["id"]),
                    )
                    conn.execute("UPDATE glossary_history SET undone = 1 WHERE id = ?", (e["id"],))
                conn.commit()
            except Exception:
                conn.rollback()
                raise
        self.bump_glossary_version()
        return len(group)

    # -- proposals from teammates ----------------------------------------------------------
    def add_proposal(self, source: str, target: str, target_lang: str, note: str, name: str, client: str,
                     kind: str = "term") -> int:
        """kind 'term' adds or changes a term; 'remove' asks for it to be deleted. Duplicates are kept once."""
        if kind == "remove":
            same = self.query_one(
                "SELECT id FROM proposals WHERE status = 'pending' AND kind = 'remove' AND lower(source) = lower(?) "
                "AND target_lang = ?",
                (source, target_lang),
            )
        else:
            same = self.query_one(
                "SELECT id FROM proposals WHERE status = 'pending' AND kind = 'term' AND lower(source) = lower(?) "
                "AND target_lang = ? AND target = ?",
                (source, target_lang, target),
            )
        if same:
            return same["id"]
        cur = self.execute(
            "INSERT INTO proposals (ts, source, target, target_lang, note, name, client, kind) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (time.time(), source, target, target_lang, note, name, client, kind),
        )
        return int(cur.lastrowid or 0)

    def pending_proposals(self) -> list[dict]:
        return self.query("SELECT id, ts, kind, source, target, target_lang, note, name FROM proposals "
                          "WHERE status = 'pending' ORDER BY id")

    def count_pending_proposals(self) -> int:
        return (self.query_one("SELECT COUNT(*) AS n FROM proposals WHERE status = 'pending'") or {}).get("n", 0)

    def get_proposal(self, proposal_id: int) -> dict | None:
        return self.query_one("SELECT * FROM proposals WHERE id = ?", (proposal_id,))

    def decide_proposal(self, proposal_id: int, status: str, term_id: int = 0) -> None:
        self.execute("UPDATE proposals SET status = ?, decided_at = ?, term_id = ? WHERE id = ?",
                     (status, time.time(), term_id, proposal_id))

    def add_term_hits(self, term_ids: Iterable[int]) -> None:
        ids = list(term_ids)
        if not ids:
            return
        with self._lock:
            self._conn.executemany(
                "UPDATE glossary SET hit_count = hit_count + 1 WHERE id = ?", [(i,) for i in ids]
            )
            self._conn.commit()

    # -- cache ---------------------------------------------------------------------
    def cache_get_many(self, keys: list[str]) -> dict[str, str]:
        if not keys:
            return {}
        found: dict[str, str] = {}
        # SQLite has a limit on bound parameters; chunk to stay well under it.
        for i in range(0, len(keys), 500):
            chunk = keys[i : i + 500]
            marks = ",".join("?" * len(chunk))
            for row in self.query(f"SELECT key, translated FROM cache WHERE key IN ({marks})", chunk):
                found[row["key"]] = row["translated"]
        if found:
            now = time.time()
            with self._lock:
                self._conn.executemany(
                    "UPDATE cache SET hits = hits + 1, last_hit_at = ? WHERE key = ?",
                    [(now, k) for k in found],
                )
                self._conn.commit()
        return found

    def cache_put_many(self, rows: list[tuple[str, str, str, str, str, str]]) -> None:
        """rows: (key, kind, source_text, translated, target_lang, model)"""
        if not rows:
            return
        now = time.time()
        with self._lock:
            self._conn.executemany(
                "INSERT OR REPLACE INTO cache (key, kind, source_text, translated, target_lang, model, hits, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, 0, ?)",
                [(*r, now) for r in rows],
            )
            self._conn.commit()

    def cache_clear(self) -> int:
        cur = self.execute("DELETE FROM cache")
        return cur.rowcount

    # -- events --------------------------------------------------------------------
    def log_event(self, **fields: Any) -> None:
        text_cols = ("endpoint", "error", "error_code")
        cols = [
            "endpoint", "items", "cache_hits", "glossary_direct", "llm_calls", "chars_in", "chars_cached",
            "terms_matched", "terms_applied", "ms", "ok", "error", "error_code",
        ]
        values = [fields.get(c, "" if c in text_cols else 0) for c in cols]
        values[cols.index("ok")] = int(bool(fields.get("ok", True)))
        values[cols.index("error")] = str(values[cols.index("error")])[:4000]
        self.execute(
            f"INSERT INTO events (ts, {', '.join(cols)}) VALUES (?, {', '.join('?' * len(cols))})",
            (time.time(), *values),
        )

    def log_call(self, model: str, items: int, status: int, ms: int, ok: bool, error_code: str = "") -> None:
        self.execute(
            "INSERT INTO upstream_calls (ts, model, items, status, ms, ok, error_code) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (time.time(), model, items, status, ms, int(ok), error_code),
        )


_db: Database | None = None


def get_db() -> Database:
    global _db
    if _db is None:
        _db = Database(DATA_DIR / "glossa.db")
    return _db


def set_db(db: Database | None) -> None:
    """Used by tests to point the app at a temporary database."""
    global _db
    _db = db
