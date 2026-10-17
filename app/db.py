"""SQLite storage: settings, glossary, translation cache and usage events."""

from __future__ import annotations

import os
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Iterable

DATA_DIR = Path(os.environ.get("DATA_DIR", "/data"))

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
    hit_count      INTEGER NOT NULL DEFAULT 0,
    created_at     REAL NOT NULL,
    updated_at     REAL NOT NULL,
    UNIQUE (source, target_lang)
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

    def upsert_term(
        self,
        source: str,
        target: str,
        target_lang: str,
        note: str = "",
        case_sensitive: bool = False,
    ) -> dict:
        now = time.time()
        self.execute(
            "INSERT INTO glossary (source, target, target_lang, note, case_sensitive, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(source, target_lang) DO UPDATE SET target = excluded.target, "
            "note = excluded.note, case_sensitive = excluded.case_sensitive, updated_at = excluded.updated_at",
            (source, target, target_lang, note, int(case_sensitive), now, now),
        )
        self.bump_glossary_version()
        return self.query_one(
            "SELECT * FROM glossary WHERE source = ? AND target_lang = ?", (source, target_lang)
        )  # type: ignore[return-value]

    def update_term(self, term_id: int, **fields: Any) -> dict | None:
        allowed = {"source", "target", "target_lang", "note", "case_sensitive"}
        sets = {k: v for k, v in fields.items() if k in allowed and v is not None}
        if not sets:
            return self.query_one("SELECT * FROM glossary WHERE id = ?", (term_id,))
        if "case_sensitive" in sets:
            sets["case_sensitive"] = int(bool(sets["case_sensitive"]))
        cols = ", ".join(f"{k} = ?" for k in sets)
        self.execute(
            f"UPDATE glossary SET {cols}, updated_at = ? WHERE id = ?",
            (*sets.values(), time.time(), term_id),
        )
        self.bump_glossary_version()
        return self.query_one("SELECT * FROM glossary WHERE id = ?", (term_id,))

    def delete_term(self, term_id: int) -> bool:
        cur = self.execute("DELETE FROM glossary WHERE id = ?", (term_id,))
        self.bump_glossary_version()
        return cur.rowcount > 0

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
