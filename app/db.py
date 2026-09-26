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
 labels TEXT NOT NULL DEFAULT '{}', employment_types TEXT NOT NULL DEFAULT '[]', source_key TEXT,
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
 source_key TEXT PRIMARY KEY, etag TEXT, last_modified TEXT, runs_date TEXT, runs_today INTEGER NOT NULL DEFAULT 0,
 last_ok_at TEXT, last_error TEXT, error_count INTEGER NOT NULL DEFAULT 0, last_new INTEGER,
 listing INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS seen_items (
 source_key TEXT NOT NULL, item_id TEXT NOT NULL, seen_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
 PRIMARY KEY (source_key, item_id)
);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS source_suggestions (
 id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL, url TEXT NOT NULL, name TEXT, why TEXT,
 origin TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'new', created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
 UNIQUE (kind, url)
);
CREATE TABLE IF NOT EXISTS profiles (
 id INTEGER PRIMARY KEY, data TEXT, resume TEXT NOT NULL DEFAULT '', resume_updated_at TEXT,
 updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
"""

# Columns added after the first version of the schema; init() adds any an older database lacks.
ADDED_COLUMNS = {
    "opportunities": {"labels": "TEXT NOT NULL DEFAULT '{}'", "employment_types": "TEXT NOT NULL DEFAULT '[]'",
                      "source_key": "TEXT"},
    "evaluations": {"passed": "INTEGER", "needs_review": "INTEGER NOT NULL DEFAULT 0",
                    "reject_reasons": "TEXT NOT NULL DEFAULT '[]'"},
    "source_state": {"last_ok_at": "TEXT", "last_error": "TEXT", "error_count": "INTEGER NOT NULL DEFAULT 0",
                     "last_new": "INTEGER", "listing": "INTEGER NOT NULL DEFAULT 0"},
}
PROFILE_ID = 1  # ponytail: one profile; add a user_id column when the app gets accounts
OP_FIELDS = ("source_url", "canonical_url", "title", "company", "description", "location", "remote",
             "salary_min", "salary_max", "currency", "contact_email", "published_at", "raw_text",
             "extracted_by", "warnings", "duplicate_of", "status", "employment_types", "source_key")


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
                        if (table, name) == ("opportunities", "source_key"):
                            # Older crawls recorded the source only in seen_items, keyed by the job's URL.
                            con.execute("UPDATE opportunities SET source_key = (SELECT s.source_key FROM seen_items s "
                                        "WHERE s.item_id IN (opportunities.source_url, opportunities.canonical_url) "
                                        "LIMIT 1)")
        con.executescript(SCHEMA)
        con.execute("DROP INDEX IF EXISTS idx_opportunities_canonical_url")  # from an early development version


def _json(value: Any, default: Any) -> str:
    return json.dumps(default if value is None else value, ensure_ascii=False)


def row_to_op(row: sqlite3.Row | None) -> dict | None:
    if row is None:
        return None
    op = dict(row)
    op["remote"] = None if op.get("remote") is None else bool(op["remote"])
    for key, default in (("extracted_by", {}), ("warnings", []), ("labels", {}), ("employment_types", [])):
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
        values["employment_types"] = _json(op.get("employment_types"), [])
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
                       show_rejected: bool = False, show_duplicates: bool = False,
                       employment: str | None = None, hide_kinds: tuple[str, ...] = ()) -> list[dict]:
    """Newest evaluation per opportunity, best score first. `kind` matches the user's label, else Laya's type.
    Unless show_rejected, rejected items and those whose kind is in hide_kinds (spam, ...) are left out;
    ignored items only show when asked for by status."""
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
    else:
        query += " AND o.status != 'ignored'"
    if not show_duplicates:
        query += " AND o.duplicate_of IS NULL"
    if not show_rejected:
        query += " AND COALESCE(e.passed, 1) = 1"
        if hide_kinds:
            query += f" AND COALESCE(kind, '') NOT IN ({', '.join('?' * len(hide_kinds))})"
            args.extend(hide_kinds)
    if needs_review:
        query += " AND e.needs_review = 1"
    if kind:
        query += " AND kind = ?"
        args.append(kind)
    if employment:
        query += " AND EXISTS (SELECT 1 FROM json_each(o.employment_types) WHERE value = ?)"
        args.append(employment)
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


def delete_opportunity(oid: int) -> None:
    """Delete it with its evaluations and drafts, and the duplicate rows that point at it."""
    with connect() as con:
        con.execute("DELETE FROM opportunities WHERE duplicate_of=?", (oid,))
        con.execute("DELETE FROM opportunities WHERE id=?", (oid,))


def labeled_opportunities() -> list[dict]:
    with connect() as con:
        return [row_to_op(r) for r in con.execute("SELECT * FROM opportunities WHERE labels != '{}' ORDER BY id")]


# --- profile and resume ----------------------------------------------------------

def get_profile() -> dict | None:
    """None until a profile is saved (a stored resume alone doesn't count)."""
    with connect() as con:
        r = con.execute("SELECT data FROM profiles WHERE id=?", (PROFILE_ID,)).fetchone()
    return json.loads(r["data"]) if r and r["data"] is not None else None


def save_profile(profile: dict) -> None:
    with connect() as con:
        con.execute("INSERT INTO profiles (id, data) VALUES (?, ?) ON CONFLICT(id) DO UPDATE SET "
                    "data=excluded.data, updated_at=CURRENT_TIMESTAMP", (PROFILE_ID, _json(profile, {})))


def get_resume() -> dict | None:
    """{"text", "updated_at"} (local time), or None when no resume is stored."""
    with connect() as con:
        r = con.execute("SELECT resume, resume_updated_at FROM profiles WHERE id=?", (PROFILE_ID,)).fetchone()
    return {"text": r["resume"], "updated_at": r["resume_updated_at"]} if r and r["resume"] else None


def save_resume(text: str) -> None:
    with connect() as con:
        con.execute("INSERT INTO profiles (id, resume, resume_updated_at) VALUES (?, ?, datetime('now', 'localtime')) "
                    "ON CONFLICT(id) DO UPDATE SET resume=excluded.resume, resume_updated_at=excluded.resume_updated_at",
                    (PROFILE_ID, text))


def delete_resume() -> None:
    with connect() as con:
        con.execute("UPDATE profiles SET resume='', resume_updated_at=NULL WHERE id=?", (PROFILE_ID,))


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

def save_draft(oid: int, kind: str, content: str, draft_id: int | None = None, status: str = "draft") -> int:
    """status is "draft" for template drafts and "ai_generated" for Ollama drafts (kept when edited)."""
    with connect() as con:
        if draft_id:
            con.execute("UPDATE drafts SET content=?, updated_at=CURRENT_TIMESTAMP WHERE id=?", (content, draft_id))
            return draft_id
        return int(con.execute("INSERT INTO drafts (opportunity_id, kind, content, status) VALUES (?,?,?,?)",
                               (oid, kind, content, status)).lastrowid)


def recent_drafts(limit: int = 5) -> list[dict]:
    with connect() as con:
        return [dict(r) for r in con.execute(
            "SELECT d.id, d.kind, d.status, d.updated_at, o.id AS opportunity_id, o.title FROM drafts d "
            "JOIN opportunities o ON o.id = d.opportunity_id ORDER BY d.updated_at DESC, d.id DESC LIMIT ?", (limit,))]


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
    return dict(r) if r else {"source_key": key, "etag": None, "last_modified": None, "runs_date": None, "runs_today": 0,
                              "last_ok_at": None, "last_error": None, "error_count": 0, "last_new": None, "listing": 0}


def put_source_state(state: dict) -> None:
    with connect() as con:
        con.execute("INSERT OR REPLACE INTO source_state (source_key, etag, last_modified, runs_date, runs_today, "
                    "last_ok_at, last_error, error_count, last_new, listing) VALUES (:source_key, :etag, :last_modified, "
                    ":runs_date, :runs_today, :last_ok_at, :last_error, :error_count, :last_new, :listing)", state)


def source_states() -> dict[str, dict]:
    with connect() as con:
        return {r["source_key"]: dict(r) for r in con.execute("SELECT * FROM source_state")}


def source_stats() -> dict[str, dict]:
    """Per source: jobs found (not counting duplicates), their average score, and what you did with them."""
    with connect() as con:
        rows = con.execute("""
          SELECT o.source_key, COUNT(*) AS found, AVG(e.score) AS avg_score,
                 SUM(o.status = 'shortlisted') AS shortlisted, SUM(o.status = 'ignored') AS ignored,
                 SUM(COALESCE(e.passed, 1) = 0) AS rejected
          FROM opportunities o
          LEFT JOIN evaluations e ON e.id = (SELECT MAX(id) FROM evaluations WHERE opportunity_id = o.id)
          WHERE o.source_key IS NOT NULL AND o.duplicate_of IS NULL
          GROUP BY o.source_key""")
        return {r["source_key"]: dict(r) for r in rows}


def is_seen(key: str, item_id: str) -> bool:
    with connect() as con:
        return con.execute("SELECT 1 FROM seen_items WHERE source_key=? AND item_id=?", (key, item_id)).fetchone() is not None


def mark_seen(key: str, item_id: str) -> None:
    with connect() as con:
        con.execute("INSERT OR IGNORE INTO seen_items (source_key, item_id) VALUES (?,?)", (key, item_id))


# --- source suggestions (from keyword suggestions and weekly research) ------------------

def add_suggestions(suggestions: list[dict], origin: str) -> int:
    """Store new suggestions; one already suggested (added or dismissed) is ignored. Returns how many were new."""
    with connect() as con:
        before = con.total_changes
        con.executemany("INSERT OR IGNORE INTO source_suggestions (kind, url, name, why, origin) VALUES (?,?,?,?,?)",
                        [(s["kind"], s["url"], s.get("name"), s.get("why"), origin) for s in suggestions])
        return con.total_changes - before


def list_suggestions() -> list[dict]:
    with connect() as con:
        return [dict(r) for r in con.execute("SELECT * FROM source_suggestions WHERE status='new' ORDER BY id DESC")]


def get_suggestion(sid: int) -> dict | None:
    with connect() as con:
        r = con.execute("SELECT * FROM source_suggestions WHERE id=?", (sid,)).fetchone()
    return dict(r) if r else None


def set_suggestion_status(sid: int, status: str) -> None:
    with connect() as con:
        con.execute("UPDATE source_suggestions SET status=? WHERE id=?", (status, sid))


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
