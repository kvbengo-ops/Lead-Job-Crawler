"""One crawl run: python -m app.crawl (scripts/run_crawl.ps1 runs it on a schedule; see PLAN.md "Scheduled runs").

sources.json is a list of sources:
  {"kind": "greenhouse", "board": "acme"}                   Greenhouse job-board API (boards.greenhouse.io/acme)
  {"kind": "lever", "board": "acme"}                        Lever postings API (jobs.lever.co/acme)
  {"kind": "rss", "url": "https://example.com/jobs.rss"}    RSS or Atom feed
  {"kind": "page", "url": "https://example.com/job/123"}    one posting page, processed once
Optional on each: "name" (shown in logs) and "max_runs_per_day" (default 3).

API and feed items are read from the structured data directly; linked pages are not fetched.
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
from datetime import date, datetime, timezone
from html import unescape
from urllib.parse import urlsplit

import feedparser
import httpx

from . import db, pipeline
from .extract import FetchError, guess_remote, fetch, finish, html_to_text, make_client

SOURCES_PATH = db.ROOT / "sources.json"
LOCK_PATH = db.ROOT / "data" / "crawl.lock"
STALE_LOCK_SECONDS = 2 * 60 * 60
KINDS = ("greenhouse", "lever", "rss", "page")
DEFAULT_RUNS_PER_DAY = 3
SAME_HOST_DELAY = 1.0  # seconds between requests to one host
MAX_LIST_BYTES = 50 * 1024 * 1024  # a big company's whole job list can exceed the 5 MB page limit
GREENHOUSE = "https://boards-api.greenhouse.io/v1/boards/{}/jobs?content=true"
LEVER = "https://api.lever.co/v0/postings/{}?mode=json"
LEVER_PER_YEAR = {"hour": 2080, "day": 260, "week": 52, "month": 12, "year": 1}


class SourceError(ValueError):
    """sources.json is malformed."""


def load_sources(path=None) -> list[dict]:
    path = path or SOURCES_PATH
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except ValueError as e:
        raise SourceError(f"sources.json is not valid JSON: {e}") from e
    if not isinstance(data, list):
        raise SourceError("sources.json must be a JSON list of sources")
    sources = []
    for n, s in enumerate(data, 1):
        if not isinstance(s, dict):
            raise SourceError(f"source #{n} must be an object")
        kind = s.get("kind")
        if kind not in KINDS:
            raise SourceError(f"source #{n}: \"kind\" must be one of {', '.join(KINDS)}")
        if kind in ("greenhouse", "lever"):
            board = str(s.get("board") or "")
            if not re.fullmatch(r"[A-Za-z0-9_-]+", board):
                raise SourceError(f"source #{n}: {kind} needs \"board\", the company's board name (letters, digits, - or _)")
            url = (GREENHOUSE if kind == "greenhouse" else LEVER).format(board)
        else:
            url = str(s.get("url") or "")
            if urlsplit(url).scheme not in ("http", "https"):
                raise SourceError(f"source #{n}: {kind} needs an http(s) \"url\"")
        runs = s.get("max_runs_per_day", DEFAULT_RUNS_PER_DAY)
        if not isinstance(runs, int) or isinstance(runs, bool) or runs < 1:
            raise SourceError(f"source #{n}: \"max_runs_per_day\" must be a whole number of at least 1")
        sources.append({"kind": kind, "url": url, "name": s.get("name") or s.get("board") or url,
                        "max_runs_per_day": runs, "key": f"{kind}:{url}", "board": s.get("board")})
    return sources


# --- turning API and feed data into opportunity records --------------------------

def _record(source: dict, **fields) -> dict:
    op = {"source_url": None, "title": None, "company": None, "description": "", "location": None, "remote": None,
          "salary_min": None, "salary_max": None, "currency": None, "contact_email": None, "published_at": None,
          "warnings": []}
    op.update(fields)
    op["raw_text"] = op["description"]
    op["extracted_by"] = {k: source["kind"] for k in ("title", "company", "description", "location", "salary_min",
                                                      "published_at") if op.get(k)}
    return finish(op)


def greenhouse_items(source: dict, data) -> list[tuple[str, dict]]:
    items = []
    for job in (data or {}).get("jobs") or []:
        # Greenhouse returns the description as HTML with its tags entity-escaped.
        description = html_to_text(unescape(job.get("content") or ""))
        location = (job.get("location") or {}).get("name")
        items.append((str(job["id"]), _record(
            source, source_url=job.get("absolute_url"), title=job.get("title"),
            company=job.get("company_name") or source["name"], description=description, location=location,
            remote=guess_remote(f"{location or ''}\n{description}"),
            published_at=job.get("first_published") or job.get("updated_at"))))
    return items


def lever_items(source: dict, data) -> list[tuple[str, dict]]:
    items = []
    for p in data or []:
        parts = [p.get("descriptionPlain") or html_to_text(p.get("description") or "")]
        for section in p.get("lists") or []:
            parts.append((section.get("text") or "") + "\n" + html_to_text(section.get("content") or ""))
        parts.append(p.get("additionalPlain") or "")
        description = "\n\n".join(x.strip() for x in parts if x and x.strip())
        workplace = (p.get("workplaceType") or "").lower()
        remote = True if workplace == "remote" else False if workplace == "onsite" else guess_remote(description)
        warnings, lo, hi, currency = [], None, None, None
        salary = p.get("salaryRange") or {}
        if salary:
            interval = (salary.get("interval") or "per-year-salary").lower()
            factor = next((f for unit, f in LEVER_PER_YEAR.items() if unit in interval), None)
            if factor is None:
                warnings.append(f"salary interval {interval} not understood; salary ignored")
            else:
                lo = salary.get("min") * factor if salary.get("min") is not None else None
                hi = salary.get("max") * factor if salary.get("max") is not None else None
                currency = salary.get("currency")
                if factor != 1:
                    warnings.append("salary given per " + next(u for u in LEVER_PER_YEAR if u in interval)
                                    + "; converted to a yearly amount")
        items.append((str(p["id"]), _record(
            source, source_url=p.get("hostedUrl"), title=p.get("text"), company=source["name"],
            description=description, location=(p.get("categories") or {}).get("location"), remote=remote,
            salary_min=lo, salary_max=hi, currency=currency, published_at=p.get("createdAt"), warnings=warnings)))
    return items


def feed_items(source: dict, content: bytes) -> list[tuple[str, dict]]:
    feed = feedparser.parse(content)
    # feedparser accepts almost anything (an HTML page parses to zero entries), so require a recognized format.
    if not feed.entries and (feed.bozo or not feed.version):
        raise FetchError(f"{source['url']} is not a readable RSS or Atom feed")
    items = []
    for e in feed.entries:
        item_id = e.get("id") or e.get("link") or e.get("title")
        if not item_id:
            continue
        body = (e.get("content") or [{}])[0].get("value") or e.get("summary") or ""
        description = html_to_text(body)
        items.append((str(item_id), _record(
            source, source_url=e.get("link"), title=e.get("title"), company=e.get("author"),
            description=description, remote=guess_remote(f"{e.get('title') or ''}\n{description}"),
            published_at=e.get("published") or e.get("updated"))))
    return items


def fetch_items(source: dict, state: dict, client: httpx.Client) -> list[tuple[str, dict | None]]:
    """Items as (item id, record); a record of None means "fetch and extract this URL" (page sources).
    Updates the ETag/Last-Modified in `state`. Returns [] when the source says nothing changed."""
    if source["kind"] == "page":
        return [(source["url"], None)]
    headers = {}
    if state.get("etag"):
        headers["If-None-Match"] = state["etag"]
    if state.get("last_modified"):
        headers["If-Modified-Since"] = state["last_modified"]
    # Official APIs are meant for programs, so robots.txt applies to feeds (on ordinary sites) only.
    r = fetch(source["url"], check_robots=source["kind"] == "rss", headers=headers, client=client,
              max_bytes=MAX_LIST_BYTES)
    if r.status_code == 304:
        return []
    state["etag"], state["last_modified"] = r.headers.get("etag"), r.headers.get("last-modified")
    if source["kind"] == "rss":
        return feed_items(source, r.content)
    try:
        data = r.json()
    except ValueError as e:
        raise FetchError(f"{source['url']} did not return JSON") from e
    return greenhouse_items(source, data) if source["kind"] == "greenhouse" else lever_items(source, data)


# --- the run ---------------------------------------------------------------------

def acquire_lock() -> bool:
    LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    for _ in range(2):
        try:
            fd = os.open(LOCK_PATH, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, f"{os.getpid()} {datetime.now(timezone.utc).isoformat()}".encode())
            os.close(fd)
            return True
        except FileExistsError:
            try:
                age = time.time() - LOCK_PATH.stat().st_mtime
            except FileNotFoundError:
                continue  # released between our two calls; try again
            if age <= STALE_LOCK_SECONDS:
                return False
            LOCK_PATH.unlink(missing_ok=True)  # left behind by a crashed run
    return False


def release_lock() -> None:
    LOCK_PATH.unlink(missing_ok=True)


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def run() -> int:
    """Returns the process exit code: 0 when everything worked, 1 when any source or item failed."""
    if not acquire_lock():
        print("Another crawl is already running; exiting.")
        return 0
    try:
        return _run()
    finally:
        release_lock()


def _run() -> int:
    db.init()
    run_log = {"started_at": now(), "sources_fetched": 0, "new_items": 0, "duplicates": 0, "pending": 0, "errors": []}

    def finish_run(code: int) -> int:
        run_log["ended_at"] = now()
        run_log["pending"] = len(db.pending_ids())
        db.record_run(run_log)
        print(f"Done: {run_log['sources_fetched']} sources fetched, {run_log['new_items']} new, "
              f"{run_log['duplicates']} duplicates, {run_log['pending']} pending evaluation, "
              f"{len(run_log['errors'])} errors.")
        return code

    try:
        profile = pipeline.load_profile()
        sources = load_sources()
    except (OSError, ValueError) as e:  # SourceError is a ValueError
        run_log["errors"].append(str(e))
        print(f"error: {e}")
        return finish_run(1)

    done, pending = pipeline.retry_pending(profile)
    if pending:
        print(f"Retried pending evaluations: {done} of {pending} evaluated.")

    today = date.today().isoformat()
    last_request: dict[str, float] = {}
    with make_client() as client:
        for source in sources:
            state = db.get_source_state(source["key"])
            if state["runs_date"] != today:
                state["runs_date"], state["runs_today"] = today, 0
            if state["runs_today"] >= source["max_runs_per_day"]:
                print(f"skip {source['name']}: already fetched {state['runs_today']} time(s) today")
                continue
            host = urlsplit(source["url"]).netloc
            wait = SAME_HOST_DELAY - (time.monotonic() - last_request.get(host, -1e9))
            if wait > 0:
                time.sleep(wait)
            try:
                items = fetch_items(source, state, client)
            except (FetchError, KeyError, TypeError, AttributeError) as e:
                run_log["errors"].append(f"{source['name']}: {e}")
                print(f"error {source['name']}: {e}")
                continue
            finally:
                last_request[host] = time.monotonic()
            state["runs_today"] += 1
            run_log["sources_fetched"] += 1
            new = 0
            for item_id, record in items:
                if db.is_seen(source["key"], item_id):
                    continue
                try:
                    oid, duplicate = pipeline.process(op=record) if record else pipeline.process(url=item_id)
                except (FetchError, ValueError) as e:
                    run_log["errors"].append(f"{source['name']}: {item_id}: {e}")  # not marked seen: retried next run
                    print(f"error {item_id}: {e}")
                    continue
                db.mark_seen(source["key"], item_id)
                run_log["duplicates" if duplicate else "new_items"] += 1
                new += 0 if duplicate else 1
                print(f"{'duplicate' if duplicate else 'added'} #{oid}: {item_id}")
            db.put_source_state(state)
            print(f"{source['name']}: {len(items)} items, {new} new")
    return finish_run(1 if run_log["errors"] else 0)


def main() -> int:
    return run()


if __name__ == "__main__":
    sys.exit(main())
