"""One crawl run: python -m app.crawl (scripts/run_crawl.ps1 runs it on a schedule; see PLAN.md "Scheduled runs").

sources.json is a list of sources:
  {"kind": "greenhouse", "board": "acme"}                   Greenhouse job-board API (boards.greenhouse.io/acme)
  {"kind": "lever", "board": "acme"}                        Lever postings API (jobs.lever.co/acme)
  {"kind": "rss", "url": "https://example.com/jobs.rss"}    RSS or Atom feed
  {"kind": "page", "url": "https://example.com/job/123"}    one posting page, processed once; a page that
                                                            lists jobs is re-read each run and new jobs added
Optional on each: "name" (shown in logs) and "max_runs_per_day" (default 3).

API and feed items are read from the structured data directly; linked pages are not fetched.
"""
from __future__ import annotations

import email
import email.policy
import imaplib
import json
import os
import re
import subprocess
import sys
import time
from datetime import date, datetime, timedelta, timezone
from html import unescape
from urllib.parse import urlencode, urlsplit

import feedparser
import httpx

from . import alerts, db, extract, feeds, ollama_client, pipeline
from .extract import FetchError, ListingPage, guess_remote, fetch, finish, html_to_text, make_client

SOURCES_PATH = db.ROOT / "sources.json"
LOCK_PATH = db.ROOT / "data" / "crawl.lock"
STALE_LOCK_SECONDS = 2 * 60 * 60
KINDS = ("greenhouse", "lever", "rss", "page", "remotive", "remoteok", "hn", "imap")
DEFAULT_RUNS_PER_DAY = 3
MAX_LIST_BYTES = 50 * 1024 * 1024  # a big company's whole job list can exceed the 5 MB page limit
GREENHOUSE = "https://boards-api.greenhouse.io/v1/boards/{}/jobs?content=true"
LEVER = "https://api.lever.co/v0/postings/{}?mode=json"
LEVER_PER_YEAR = {"hour": 2080, "day": 260, "week": 52, "month": 12, "year": 1}
REMOTIVE_MAX_RUNS = 4  # Remotive's terms: at most 4 requests a day
IMAP_DAYS = 14  # how far back alert emails are read
NOTIFY_SCRIPT = db.ROOT / "scripts" / "notify.ps1"
DASHBOARD = "http://127.0.0.1:8100"
DEFAULT_NOTIFY_SCORE = 80
MAX_DRAFTS_PER_RUN = 3  # about 20 s each with qwen2.5:3b
# Hacker News monthly threads: where to find the latest one, and which top-level comments are jobs or leads.
HN_SEARCH = "https://hn.algolia.com/api/v1/search_by_date"
HN_COMMENTS = "https://hn.algolia.com/api/v1/search?tags=comment,story_{}&hitsPerPage=1000"
HN_THREADS = {
    # thread: (search parameters, title the story starts with, comments to keep)
    "who_is_hiring": ({"tags": "story,author_whoishiring", "query": "who is hiring"}, "Ask HN: Who is hiring?", None),
    "seeking_freelancer": ({"tags": "story", "query": "Freelancer? Seeking freelancer?"},
                           "Ask HN: Freelancer? Seeking freelancer?", re.compile(r"^\W*seeking freelancer", re.I)),
}


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
        url = source_url(s)
        if kind == "hn":
            if s.get("thread") not in HN_THREADS:
                raise SourceError(f"source #{n}: hn needs \"thread\": one of {', '.join(HN_THREADS)}")
        elif kind == "imap":
            if not str(s.get("label") or "").strip():
                raise SourceError(f"source #{n}: imap needs \"label\", the mail folder or Gmail label with job alerts")
        elif kind in ("greenhouse", "lever"):
            if not re.fullmatch(r"[A-Za-z0-9_-]+", str(s.get("board") or "")):
                raise SourceError(f"source #{n}: {kind} needs \"board\", the company's board name (letters, digits, - or _)")
        elif urlsplit(url).scheme not in ("http", "https"):
            raise SourceError(f"source #{n}: {kind} needs an http(s) \"url\"")
        runs = s.get("max_runs_per_day", DEFAULT_RUNS_PER_DAY)
        if not isinstance(runs, int) or isinstance(runs, bool) or runs < 1:
            raise SourceError(f"source #{n}: \"max_runs_per_day\" must be a whole number of at least 1")
        if kind == "remotive":
            runs = min(runs, REMOTIVE_MAX_RUNS)
        sources.append({"kind": kind, "url": url,
                        "name": s.get("name") or s.get("board") or s.get("thread") or s.get("label") or url,
                        "max_runs_per_day": runs, "key": f"{kind}:{url}", "board": s.get("board"),
                        "paused": bool(s.get("paused")), "thread": s.get("thread"), "label": s.get("label")})
    return sources


def source_url(s: dict) -> str:
    """The URL a sources.json entry is fetched from. Its key in the database is f"{kind}:{url}"."""
    if s.get("kind") in ("greenhouse", "lever"):
        return (GREENHOUSE if s["kind"] == "greenhouse" else LEVER).format(s.get("board") or "")
    if s.get("kind") == "hn":
        return f"https://news.ycombinator.com/#{s.get('thread')}"  # an identifier: the thread is looked up each run
    if s.get("kind") == "imap":
        return f"imap://{s.get('label')}"  # an identifier: the mailbox comes from IMAP_HOST and IMAP_USER
    return str(s.get("url") or "")


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
            employment_types=(p.get("categories") or {}).get("commitment"),  # e.g. "Full-time", "Contract"
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
    if source["kind"] == "hn":
        return hn_items(source, client)
    if source["kind"] == "imap":
        return imap_items(source, client)
    if source["kind"] == "page":
        if not state.get("listing"):
            return [(source["url"], None)]  # one posting, or a list page not seen yet: the run loop finds out
        try:
            extract.from_url(source["url"], min_links=1)
        except ListingPage as listing:
            return [(url, None) for url, _ in listing.links]
        # A list page that stops linking to jobs is behind a login wall or has a new layout. Storing it as a job
        # would also mark it seen, and the source would never be read again.
        raise FetchError("the list page shows no job links (a login wall, or the site changed its layout?)")
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
    data = _json(r, source["url"])
    if source["kind"] in ("remotive", "remoteok"):
        parse = feeds.remotive_items if source["kind"] == "remotive" else feeds.remoteok_items
        return [(item_id, _record(source, **fields)) for item_id, fields in parse(source, data)]
    return greenhouse_items(source, data) if source["kind"] == "greenhouse" else lever_items(source, data)


def _json(r: httpx.Response, url: str):
    try:
        return r.json()
    except ValueError as e:
        raise FetchError(f"{url} did not return JSON") from e


def imap_items(source: dict, client: httpx.Client) -> list[tuple[str, dict | None]]:
    """Jobs from the last IMAP_DAYS of alert emails in a mail label, read-only. A job whose page robots.txt
    lets us fetch is fetched in full; otherwise (LinkedIn, for one) it's scored from the email's own text."""
    host = os.environ.get("IMAP_HOST", "imap.gmail.com")
    user, password = os.environ.get("IMAP_USER"), os.environ.get("IMAP_PASSWORD")
    if not user or not password:
        raise FetchError("IMAP_USER and IMAP_PASSWORD aren't set (see the README, \"Email alerts\")")
    since = (date.today() - timedelta(days=IMAP_DAYS)).strftime("%d-%b-%Y")
    messages = []
    try:
        with imaplib.IMAP4_SSL(host, timeout=30) as box:
            box.login(user, password)
            status, _ = box.select(f'"{source["label"]}"', readonly=True)
            if status != "OK":
                raise FetchError(f"no mail label called {source['label']!r} in {user}")
            _, found = box.search(None, "SINCE", since)
            for number in found[0].split():
                _, parts = box.fetch(number, "(RFC822)")
                messages.append(email.message_from_bytes(parts[0][1], policy=email.policy.default))
    except (imaplib.IMAP4.error, OSError) as e:
        raise FetchError(f"could not read mail from {host}: {e}") from e
    items = []
    for message in messages:
        for item_id, fields in alerts.alert_items(message):
            url = fields.get("source_url") or item_id
            fetchable = urlsplit(url).scheme in ("http", "https") and extract.robots_allowed(url, client)
            items.append((url, None) if fetchable else (item_id, _record(source, **fields)))
    return items


def hn_items(source: dict, client: httpx.Client) -> list[tuple[str, dict]]:
    """Top-level comments of the latest monthly thread; for the freelancer thread, only clients' posts."""
    params, title, keep = HN_THREADS[source["thread"]]
    url = f"{HN_SEARCH}?{urlencode({**params, 'hitsPerPage': 20})}"
    stories = _json(fetch(url, check_robots=False, client=client), url).get("hits") or []
    story = next((h for h in stories if (h.get("title") or "").startswith(title)), None)
    if story is None:
        raise FetchError(f"no Hacker News thread titled {title!r} found")
    url = HN_COMMENTS.format(story["objectID"])
    hits = _json(fetch(url, check_robots=False, client=client, max_bytes=MAX_LIST_BYTES), url).get("hits") or []
    top = [h for h in hits if str(h.get("parent_id")) == str(story["objectID"])
           and (keep is None or keep.search(html_to_text(unescape(h.get("comment_text") or ""))))]
    return [(item_id, _record(source, **fields)) for item_id, fields in feeds.hn_items(source, {"hits": top})]


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
    new_ids: list[int] = []
    newly_failing: list[str] = []  # sources that reached 2 failures in a row this run
    with make_client() as client:
        for source in sources:
            state = db.get_source_state(source["key"])
            if state["runs_date"] != today:
                state["runs_date"], state["runs_today"] = today, 0
            if source["paused"]:
                print(f"skip {source['name']}: paused")
                continue
            if state["runs_today"] >= source["max_runs_per_day"]:
                print(f"skip {source['name']}: already fetched {state['runs_today']} time(s) today")
                continue
            host = urlsplit(source["url"]).netloc
            wait = extract.SAME_HOST_DELAY - (time.monotonic() - last_request.get(host, -1e9))
            if wait > 0:
                time.sleep(wait)
            try:
                items = fetch_items(source, state, client)
            except (FetchError, KeyError, TypeError, AttributeError) as e:
                run_log["errors"].append(f"{source['name']}: {e}")
                print(f"error {source['name']}: {e}")
                state["error_count"] += 1
                state["last_error"] = str(e)[:300]
                db.put_source_state(state)
                if state["error_count"] == 2:
                    newly_failing.append(f"{source['name']}: {e}")
                continue
            finally:
                last_request[host] = time.monotonic()
            state["runs_today"] += 1
            run_log["sources_fetched"] += 1
            new = 0
            for item_id, record in items:
                if db.is_seen(source["key"], item_id):
                    continue
                if record is None and item_id != source.get("url"):
                    time.sleep(extract.SAME_HOST_DELAY)  # a job found on a list page: same site as the last request
                try:
                    oid, duplicate = pipeline.process(op=record, url=None if record else item_id, source=source["key"])
                except ListingPage as listing:
                    if item_id == source.get("url"):
                        # The page source is a list of jobs: this loop goes on to its jobs. The list itself is
                        # never marked seen, so the next run reads it again and picks up new jobs.
                        items.extend((url, None) for url, _ in listing.links)
                        state["listing"] = 1
                        continue
                    run_log["errors"].append(f"{source['name']}: {item_id}: {listing}")
                    continue
                except (FetchError, ValueError) as e:
                    run_log["errors"].append(f"{source['name']}: {item_id}: {e}")  # not marked seen: retried next run
                    print(f"error {item_id}: {e}")
                    continue
                db.mark_seen(source["key"], item_id)
                run_log["duplicates" if duplicate else "new_items"] += 1
                new += 0 if duplicate else 1
                new_ids += [] if duplicate else [oid]
                print(f"{'duplicate' if duplicate else 'added'} #{oid}: {item_id}")
            state.update(last_ok_at=now(), last_error=None, error_count=0, last_new=new)
            db.put_source_state(state)
            print(f"{source['name']}: {len(items)} items, {new} new")
    alert_user(new_ids, newly_failing, profile)
    return finish_run(1 if run_log["errors"] else 0)


def alert_user(new_ids: list[int], failing: list[str], profile: dict) -> None:
    """Write AI drafts for this run's strongest matches, then show one notification about them and about
    sources that just started failing."""
    threshold = profile.get("notify_score", DEFAULT_NOTIFY_SCORE)
    strong = []
    for oid in new_ids:
        ev = db.latest_evaluation(oid)
        if ev and ev["passed"] and ev["score"] is not None and ev["score"] >= threshold:
            strong.append((ev["score"], oid, ((ev["result"].get("answers") or {}).get("type") or {}).get("value")))
    strong.sort(reverse=True)
    drafted = 0
    for _, oid, kind in strong[:MAX_DRAFTS_PER_RUN]:
        draft_kind = "lead_email" if kind == "lead" else "job_application"
        try:
            text = ollama_client.generate_draft(db.get_opportunity(oid), profile, draft_kind,
                                                resume=pipeline.load_resume())
        except ollama_client.OllamaError as e:
            print(f"No AI drafts this run: {e}")
            break
        db.save_draft(oid, draft_kind, text, status="ai_generated")
        drafted += 1
    title, lines, url = "", [], DASHBOARD
    if strong:
        best = db.get_opportunity(strong[0][1])
        title = f"{len(strong)} new strong match{'' if len(strong) == 1 else 'es'}"
        lines.append(f"Best: {best['title'] or 'Untitled opportunity'} ({strong[0][0]:.0f})")
        if drafted:
            lines.append(f"{drafted} AI draft{'' if drafted == 1 else 's'} ready to review")
        url = f"{DASHBOARD}/opportunities/{strong[0][1]}" if len(strong) == 1 else f"{DASHBOARD}/opportunities?status=new"
    if failing:
        title = title or "A crawler source keeps failing"
        lines += [f"Failing: {f}" for f in failing]
        url = url if strong else f"{DASHBOARD}/sources"
    if lines:
        notify(title, "\n".join(lines), url)


def notify(title: str, body: str, url: str) -> None:
    """Show a Windows notification with scripts/notify.ps1 (Codex's X11). A missing or failing script only
    prints: a notification problem never fails the crawl."""
    if os.name != "nt" or not NOTIFY_SCRIPT.exists():
        print(f"notification not shown: {title}: {body}")
        return
    try:
        r = subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(NOTIFY_SCRIPT),
                            "-Title", title, "-Body", body, "-Url", url], capture_output=True, text=True, timeout=30)
        if r.returncode or r.stderr.strip():
            print(f"notification failed: {r.stderr.strip()[:300]}")
    except (OSError, subprocess.TimeoutExpired) as e:
        print(f"notification failed: {e}")


def main() -> int:
    return run()


if __name__ == "__main__":
    sys.exit(main())
