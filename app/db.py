"""SQLite storage in one file, data/app.db (kept out of git)."""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / "data" / "app.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS opportunities (
 id INTEGER PRIMARY KEY AUTOINCREMENT, source_url TEXT, canonical_url TEXT,
 title TEXT, company TEXT, description TEXT NOT NULL DEFAULT '', location TEXT,
 remote INTEGER, salary_min REAL, salary_max REAL, currency TEXT,
 contact_email TEXT, published_at TEXT, raw_text TEXT NOT NULL DEFAULT '',
 extracted_by TEXT NOT NULL DEFAULT '{}', warnings TEXT NOT NULL DEFAULT '[]',
 duplicate_of INTEGER, status TEXT NOT NULL DEFAULT 'new', created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
 labels TEXT NOT NULL DEFAULT '{}',
 FOREIGN KEY (duplicate_of) REFERENCES opportunities(id)
);
CREATE INDEX IF NOT EXISTS idx_opportunities_canonical ON opportunities(canonical_url);
CREATE TABLE IF NOT EXISTS evaluations (
 id INTEGER PRIMARY KEY AUTOINCREMENT, opportunity_id INTEGER NOT NULL, result TEXT NOT NULL,
 score REAL, components TEXT NOT NULL DEFAULT '{}', reasons TEXT NOT NULL DEFAULT '[]',
 created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
 passed INTEGER, needs_review INTEGER NOT NULL DEFAULT 0, reject_reasons TEXT NOT NULL DEFAULT '[]',
 FOREIGN KEY (opportunity_id) REFERENCES opportunities(id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS drafts (
 id INTEGER PRIMARY KEY AUTOINCREMENT, opportunity_id INTEGER NOT NULL, kind TEXT NOT NULL,
 content TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'draft', created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
 updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
 FOREIGN KEY (opportunity_id) REFERENCES opportunities(id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS crawl_runs (
 id INTEGER PRIMARY KEY AUTOINCREMENT, started_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
 ended_at TEXT, sources_fetched INTEGER NOT NULL DEFAULT 0, new_items INTEGER NOT NULL DEFAULT 0,
 duplicates INTEGER NOT NULL DEFAULT 0, pending INTEGER NOT NULL DEFAULT 0, errors TEXT NOT NULL DEFAULT '[]'
);
CREATE TABLE IF NOT EXISTS source_state (
 source_key TEXT PRIMARY KEY, etag TEXT, last_modified TEXT, runs_date TEXT, runs_today INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS seen_items (
 source_key TEXT NOT NULL, item_id TEXT NOT NULL, seen_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
 PRIMARY KEY (source_key, item_id)
);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
"""

# Columns added after the first version of the schema; init() adds any an older database lacks.
ADDED_COLUMNS = {
    "opportunities": {"labels": "TEXT NOT NULL DEFAULT '{}'"},
    "evaluations": {"passed": "INTEGER", "needs_review": "INTEGER NOT NULL DEFAULT 0",
                    "reject_reasons": "TEXT NOT NULL DEFAULT '[]'"},
}
OP_FIELDS = ("source_url", "canonical_url", "title", "company", "description", "location", "remote",
             "salary_min", "salary_max", "currency", "contact_email", "published_at", "raw_text",
             "extracted_by", "warnings", "duplicate_of", "status")


@contextmanager
def connect() -> Iterator[sqlite3.Connection]:
    """Commit on success, roll back on error, and always close (sqlite3's own context manager never closes)."""
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys=ON")
    try:
        yield con
        con.commit()
    except BaseException:
        con.rollback()
        raise
    finally:
        con.close()


def init() -> None:
    with connect() as con:
        for table, columns in ADDED_COLUMNS.items():
            if con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone():
                have = {r["name"] for r in con.execute(f"PRAGMA table_info({table})")}
                for name, decl in columns.items():
                    if name not in have:
                        con.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")
        con.executescript(SCHEMA)
        con.execute("DROP INDEX IF EXISTS idx_opportunities_canonical_url")  # from an early development version


def _json(value: Any, default: Any) -> str:
    return json.dumps(default if value is None else value, ensure_ascii=False)


def row_to_op(row: sqlite3.Row | None) -> dict | None:
    if row is None:
        return None
    op = dict(row)
    op["remote"] = None if op.get("remote") is None else bool(op["remote"])
    for key, default in (("extracted_by", {}), ("warnings", []), ("labels", {})):
        op[key] = json.loads(op.get(key) or json.dumps(default))
    return op


# --- opportunities ------------------------------------------------------------

def insert_opportunity(op: dict[str, Any]) -> tuple[int, bool]:
    """Insert and dedupe by canonical URL. A repeat is stored as its own row pointing at the original."""
    with connect() as con:
        existing = None
        if op.get("canonical_url"):
            existing = con.execute("SELECT id FROM opportunities WHERE canonical_url=? AND duplicate_of IS NULL "
                                   "ORDER BY id LIMIT 1", (op["canonical_url"],)).fetchone()
        values = {k: op.get(k) for k in OP_FIELDS}
        values["remote"] = None if op.get("remote") is None else int(bool(op["remote"]))
        values["extracted_by"] = _json(op.get("extracted_by"), {})
        values["warnings"] = _json(op.get("warnings"), [])
        values["status"] = op.get("status") or "new"
        if existing:
            values["duplicate_of"], values["status"] = int(existing["id"]), "ignored"
        cols = ", ".join(OP_FIELDS)
        cur = con.execute(f"INSERT INTO opportunities ({cols}) VALUES ({', '.join(':' + k for k in OP_FIELDS)})",
                          values)
        return int(cur.lastrowid), existing is not None


def find_original(canonical_url: str | None) -> int | None:
    if not canonical_url:
        return None
    with connect() as con:
        r = con.execute("SELECT id FROM opportunities WHERE canonical_url=? AND duplicate_of IS NULL ORDER BY id LIMIT 1",
                        (canonical_url,)).fetchone()
    return int(r["id"]) if r else None


def get_opportunity(oid: int) -> dict | None:
    with connect() as con:
        return row_to_op(con.execute("SELECT * FROM opportunities WHERE id=?", (oid,)).fetchone())


def set_status(oid: int, status: str) -> None:
    with connect() as con:
        con.execute("UPDATE opportunities SET status=? WHERE id=?", (status, oid))


def set_labels(oid: int, labels: dict) -> None:
    with connect() as con:
        con.execute("UPDATE opportunities SET labels=? WHERE id=?", (_json(labels, {}), oid))


def pending_ids() -> list[int]:
    with connect() as con:
        return [r["id"] for r in con.execute("SELECT id FROM opportunities WHERE status='pending_evaluation' ORDER BY id")]


def list_opportunities(status: str | None = None, kind: str | None = None, needs_review: bool = False,
                       show_rejected: bool = False, show_duplicates: bool = False) -> list[dict]:
    """Newest evaluation per opportunity, best score first. `kind` matches the user's label, else Laya's type."""
    query = """
      SELECT o.*, e.score, e.passed, e.needs_review, e.reject_reasons,
             COALESCE(json_extract(o.labels, '$.type'), json_extract(e.result, '$.answers.type.value')) AS kind
      FROM opportunities o
      LEFT JOIN evaluations e ON e.id = (SELECT MAX(id) FROM evaluations WHERE opportunity_id = o.id)
      WHERE 1=1"""
    args: list[Any] = []
    if status:
        query += " AND o.status=?"
        args.append(status)
    if not show_duplicates:
        query += " AND o.duplicate_of IS NULL"
    if not show_rejected:
        query += " AND COALESCE(e.passed, 1) = 1"
    if needs_review:
        query += " AND e.needs_review = 1"
    if kind:
        query += " AND kind = ?"
        args.append(kind)
    query += " ORDER BY e.score IS NULL, e.score DESC, o.id DESC"
    with connect() as con:
        rows = []
        for r in con.execute(query, args):
            op = row_to_op(r)
            op["reject_reasons"] = json.loads(r["reject_reasons"] or "[]")
            rows.append(op)
        return rows


def count_created_since(timestamp: str | None) -> int:
    with connect() as con:
        if not timestamp:
            return con.execute("SELECT COUNT(*) FROM opportunities WHERE duplicate_of IS NULL").fetchone()[0]
        return con.execute("SELECT COUNT(*) FROM opportunities WHERE duplicate_of IS NULL AND created_at > ?",
                           (timestamp,)).fetchone()[0]


def labeled_opportunities() -> list[dict]:
    with connect() as con:
        return [row_to_op(r) for r in con.execute("SELECT * FROM opportunities WHERE labels != '{}' ORDER BY id")]


# --- evaluations --------------------------------------------------------------

def save_evaluation(oid: int, evaluation: dict, result: dict | None = None) -> None:
    """`result` is score()'s output; None when Laya failed and the opportunity is pending."""
    result = result or {}
    with connect() as con:
        con.execute(
            "INSERT INTO evaluations (opportunity_id, result, score, components, reasons, passed, needs_review, "
            "reject_reasons) VALUES (?,?,?,?,?,?,?,?)",
            (oid, _json(evaluation, {}), result.get("score"), _json(result.get("components"), {}),
             _json(result.get("reasons"), []), None if "passed" not in result else int(result["passed"]),
             int(bool(result.get("needs_review"))), _json(result.get("reject_reasons"), [])))


def latest_evaluation(oid: int) -> dict | None:
    with connect() as con:
        r = con.execute("SELECT * FROM evaluations WHERE opportunity_id=? ORDER BY id DESC LIMIT 1", (oid,)).fetchone()
    if r is None:
        return None
    ev = dict(r)
    for key, default in (("result", {}), ("components", {}), ("reasons", []), ("reject_reasons", [])):
        ev[key] = json.loads(ev.get(key) or json.dumps(default))
    return ev


# --- drafts -------------------------------------------------------------------

def save_draft(oid: int, kind: str, content: str, draft_id: int | None = None) -> int:
    with connect() as con:
        if draft_id:
            con.execute("UPDATE drafts SET content=?, updated_at=CURRENT_TIMESTAMP WHERE id=?", (content, draft_id))
            return draft_id
        return int(con.execute("INSERT INTO drafts (opportunity_id, kind, content) VALUES (?,?,?)",
                               (oid, kind, content)).lastrowid)


def get_draft(draft_id: int) -> dict | None:
    with connect() as con:
        r = con.execute("SELECT * FROM drafts WHERE id=?", (draft_id,)).fetchone()
    return dict(r) if r else None


def list_drafts(oid: int) -> list[dict]:
    with connect() as con:
        return [dict(r) for r in con.execute("SELECT * FROM drafts WHERE opportunity_id=? ORDER BY id DESC", (oid,))]


# --- crawl state --------------------------------------------------------------

def get_source_state(key: str) -> dict:
    with connect() as con:
        r = con.execute("SELECT * FROM source_state WHERE source_key=?", (key,)).fetchone()
    return dict(r) if r else {"source_key": key, "etag": None, "last_modified": None, "runs_date": None, "runs_today": 0}


def put_source_state(state: dict) -> None:
    with connect() as con:
        con.execute("INSERT OR REPLACE INTO source_state (source_key, etag, last_modified, runs_date, runs_today) "
                    "VALUES (:source_key, :etag, :last_modified, :runs_date, :runs_today)", state)


def is_seen(key: str, item_id: str) -> bool:
    with connect() as con:
        return con.execute("SELECT 1 FROM seen_items WHERE source_key=? AND item_id=?", (key, item_id)).fetchone() is not None


def mark_seen(key: str, item_id: str) -> None:
    with connect() as con:
        con.execute("INSERT OR IGNORE INTO seen_items (source_key, item_id) VALUES (?,?)", (key, item_id))


def record_run(run: dict) -> None:
    with connect() as con:
        con.execute("INSERT INTO crawl_runs (started_at, ended_at, sources_fetched, new_items, duplicates, pending, errors) "
                    "VALUES (?,?,?,?,?,?,?)", (run["started_at"], run["ended_at"], run["sources_fetched"],
                                               run["new_items"], run["duplicates"], run["pending"],
                                               _json(run["errors"], [])))


def last_run() -> dict | None:
    with connect() as con:
        r = con.execute("SELECT * FROM crawl_runs ORDER BY id DESC LIMIT 1").fetchone()
    if r is None:
        return None
    run = dict(r)
    run["errors"] = json.loads(run["errors"] or "[]")
    return run


def get_meta(key: str) -> str | None:
    with connect() as con:
        r = con.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return r["value"] if r else None


def set_meta(key: str, value: str) -> None:
    with connect() as con:
        con.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?,?)", (key, value))


def swap_timestamp(key: str) -> str | None:
    """Store the current time under `key` (in created_at's format) and return the previous value."""
    with connect() as con:
        r = con.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        con.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, CURRENT_TIMESTAMP)", (key,))
    return r["value"] if r else None
