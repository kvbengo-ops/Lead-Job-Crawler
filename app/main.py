"""Local dashboard: python -m uvicorn app.main:app --host 127.0.0.1 --port 8100 (scripts/start.ps1 does this)."""
from __future__ import annotations

import json
import os
import tempfile
import time
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse, Response
from jinja2 import Environment, FileSystemLoader
from markupsafe import Markup

from . import crawl, db, drafts, extract, keywords, ollama_client, pipeline, resume
from .extract import MAX_LISTING_LINKS, FetchError, ListingPage
from .normalize import canonical_url, strip_boilerplate
from .normalize import EMPLOYMENT_TYPES

EMPLOYMENT_LABELS = {"full_time": "Full-time", "part_time": "Part-time", "contract": "Contract",
                     "freelance": "Freelance", "internship": "Internship", "flexible_hours": "Flexible hours"}

app = FastAPI(title="Local Job and Lead Crawler")
db.init()

@app.get("/favicon.svg")
def favicon() -> Response:
    svg = """<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 64 64'><defs><linearGradient id='g' x1='0' y1='0' x2='1' y2='1'><stop offset='0' stop-color='#7c5cff'/><stop offset='.55' stop-color='#4f7bff'/><stop offset='1' stop-color='#22c1ee'/></linearGradient></defs><rect width='64' height='64' rx='14' fill='url(#g)'/><path d='M18 18h28v7H26v7h16v7H26v7h20v7H18z' fill='white'/></svg>"""
    return Response(svg, media_type="image/svg+xml")

# autoescape: titles and descriptions come from scraped pages and must never be rendered as HTML.
templates = Environment(loader=FileSystemLoader(Path(__file__).parent / "templates"), autoescape=True)
# Icons are <symbol>s in base.html; icon("star") draws one. Names are only ever written in templates.
templates.globals["icon"] = lambda name: Markup(f'<svg class="i" aria-hidden="true"><use href="#i-{name}"/></svg>')
templates.filters["ts"] = lambda value: (value or "")[:16].replace("T", " ")  # "2026-09-26 08:01"
templates.filters["host"] = lambda url: urlsplit(url or "").netloc.removeprefix("www.")
def excerpt(o: dict, limit: int = 220) -> str:
    """Start of the description without navigation lines or a repeat of the title."""
    text = " ".join(strip_boilerplate(o.get("description") or "", o.get("title")).split())
    return text if len(text) <= limit else text[:limit].rsplit(" ", 1)[0] + "…"


templates.filters["excerpt"] = excerpt

STATUSES = ["new", "shortlisted", "ignored", "pending_evaluation"]
USER_STATUSES = {"new", "shortlisted", "ignored"}
DRAFT_KINDS = {"job_application", "lead_email"}


def render(name: str, status_code: int = 200, **context) -> HTMLResponse:
    context.setdefault("employment_labels", EMPLOYMENT_LABELS)
    return HTMLResponse(templates.get_template(name).render(**context), status_code=status_code)


def profile_or_empty() -> dict:
    try:
        return pipeline.load_profile()
    except (OSError, ValueError):
        return {}


def hidden_kinds(profile: dict) -> tuple[str, ...]:
    """Types that aren't real opportunities (spam, irrelevant): kept out of the list unless asked for."""
    return tuple(profile.get("reject_types") or ())


def criteria(profile: dict, question: str) -> list[str]:
    return list(((profile.get("questions") or {}).get(question) or {}).get("criteria") or {})


def get_op_or_404(oid: int) -> dict:
    op = db.get_opportunity(oid)
    if op is None:
        raise HTTPException(404, "No such opportunity")
    return op


@app.middleware("http")
async def same_origin_posts(request: Request, call_next):
    # The dashboard only listens on 127.0.0.1, but any web page open in the browser could still post a
    # form to it. Browsers send Origin on cross-site posts; refuse those.
    if request.method == "POST":
        origin = request.headers.get("origin")
        if origin and origin != "null" and urlsplit(origin).netloc != request.headers.get("host"):
            return PlainTextResponse("Cross-site form posts are not allowed.", status_code=403)
    return await call_next(request)


@app.get("/health")
def health():
    return {"ok": True}


@app.get("/", response_class=HTMLResponse)
def dashboard():
    previous_visit = db.swap_timestamp("last_visit")
    # ponytail: counts come from one list query in Python; move them to SQL GROUP BY if the list gets huge.
    rows = db.list_opportunities(hide_kinds=hidden_kinds(profile_or_empty()))
    saved = db.get_profile() or {}
    sources = _read_sources()
    setup = [("Fill in your profile", "Your skills drive every score.", "/profile",
              bool(saved.get("name") and saved.get("skills"))),
             ("Upload your resume", "AI drafts use your full history.", "/profile", bool(pipeline.load_resume())),
             ("Add a source", "The crawler checks it for new posts.", "/sources", bool(sources))]
    return render("dashboard.html", user_name=saved.get("name") or "",
                  new_count=db.count_created_since(previous_visit) if previous_visit else 0,
                  stats={"total": len(rows),
                         "shortlisted": sum(r["status"] == "shortlisted" for r in rows),
                         "review": sum(bool(r["needs_review"]) and r["status"] != "ignored" for r in rows),
                         "pending": sum(r["status"] == "pending_evaluation" for r in rows)},
                  top=[r for r in rows if r["status"] == "new"][:5], drafts=db.recent_drafts(5),
                  last_run=db.last_run(), source_count=len(sources), failing=failing_sources(sources),
                  setup=[] if all(step[-1] for step in setup) else setup)


# Status tabs on the list: label and the query they set; the other filters are kept.
TABS = [("All", {}), ("New", {"status": "new"}), ("Shortlisted", {"status": "shortlisted"}),
        ("Needs review", {"review": "1"}), ("Pending", {"status": "pending_evaluation"}),
        ("Ignored", {"status": "ignored"})]


@app.get("/opportunities", response_class=HTMLResponse)
def opportunities(request: Request, status: str = "", kind: str = "", review: str = "", rejected: str = "",
                  employment: str = "", added: int | None = None, deleted: str = ""):
    profile = profile_or_empty()
    rows = db.list_opportunities(status=status if status in STATUSES else None, kind=kind or None,
                                 needs_review=bool(review), show_rejected=bool(rejected),
                                 employment=employment if employment in EMPLOYMENT_TYPES else None,
                                 hide_kinds=hidden_kinds(profile))
    f = {"status": status, "kind": kind, "review": review, "rejected": rejected, "employment": employment}
    keep = {k: v for k, v in f.items() if k in ("kind", "employment", "rejected") and v}
    current = {k: v for k, v in f.items() if k in ("status", "review") and v}
    tabs = [(label, "/opportunities?" + urlencode({**keep, **q}), q == current) for label, q in TABS]
    here = request.url.path + (f"?{request.url.query}" if request.url.query else "")
    notice = ("Opportunity deleted." if deleted else
              None if added is None else f"Added {added} job{'' if added == 1 else 's'} from the list.")
    return render("list.html", rows=rows, kinds=criteria(profile, "type"), notice=notice,
                  f=f, tabs=tabs, here=here, filtered=bool(keep or current))


@app.get("/add", response_class=HTMLResponse)
def add_form():
    return render("add.html", url="", text="", error=None)


@app.post("/add")
def add(url: str = Form(""), text: str = Form("")):
    try:
        oid, duplicate = pipeline.process(text=text, url=url or None)
    except ListingPage as listing:
        # A job board's list page: offer its jobs one by one instead of storing the whole page.
        jobs = [{"url": u, "name": name, "have": db.find_original(canonical_url(u))} for u, name in listing.links]
        return render("add.html", url=url, text=text, error=None, jobs=jobs)
    except (FetchError, ValueError) as e:
        return render("add.html", status_code=422, url=url, text=text, error=str(e))
    if duplicate:
        original = db.get_opportunity(oid)["duplicate_of"] or oid
        return RedirectResponse(f"/opportunities/{original}?notice=duplicate", status_code=303)
    return RedirectResponse(f"/opportunities/{oid}", status_code=303)


@app.post("/add/many")
def add_many(urls: list[str] = Form([])):
    """Add the jobs picked from a list page, one fetch at a time so the site isn't hammered."""
    added, errors = 0, []
    for i, url in enumerate(urls[:MAX_LISTING_LINKS]):
        if i:
            time.sleep(extract.SAME_HOST_DELAY)
        try:
            _, duplicate = pipeline.process(url=url)
        except (FetchError, ValueError, ListingPage) as e:
            errors.append(str(e))
            continue
        added += not duplicate
    if errors:
        return render("add.html", status_code=422, url="", text="", error=None, failures=errors, added=added)
    return RedirectResponse(f"/opportunities?status=new&added={added}", status_code=303)


NOTICES = {
    "duplicate": ("This URL is already in your list; here is the original.", False),
    "laya_down": ("Laya didn't answer, so this is marked pending and will be retried on the next crawl.", True),
    "evaluated": ("Re-evaluated with Laya.", False),
    "labels": ("Labels saved.", False),
}


@app.get("/opportunities/{oid}", response_class=HTMLResponse)
def detail(oid: int, notice: str = ""):
    text, is_error = NOTICES.get(notice, (None, False))
    return detail_page(oid, text, is_error)


def detail_page(oid: int, notice: str | None = None, notice_error: bool = False, status_code: int = 200):
    op = get_op_or_404(oid)
    ev = db.latest_evaluation(oid)
    profile = profile_or_empty()
    link = op["source_url"] if op["source_url"] and urlsplit(op["source_url"]).scheme in ("http", "https") else None
    return render("detail.html", status_code=status_code, o=op, ev=ev,
                  answers=(ev or {}).get("result", {}).get("answers") or {},
                  weights=profile.get("score_weights") or {}, threshold=profile.get("confidence_threshold", 0.6),
                  type_options=criteria(profile, "type"), work_mode_options=criteria(profile, "work_mode"),
                  drafts=db.list_drafts(oid), source_link=link, notice=notice, notice_error=notice_error,
                  ai_model=ollama_client.MODEL)


@app.post("/opportunities/{oid}/status")
def set_status(oid: int, status: str = Form(...), next: str = Form("")):
    get_op_or_404(oid)
    if status not in USER_STATUSES:
        raise HTTPException(400, "Unknown status")
    db.set_status(oid, status)
    # Only same-site paths: browsers treat "//host" and "/\host" as another site.
    local = next.startswith("/") and next[1:2] not in ("/", "\\")
    return RedirectResponse(next if local else f"/opportunities/{oid}", status_code=303)


@app.post("/opportunities/{oid}/delete")
def delete_opportunity(oid: int):
    get_op_or_404(oid)
    db.delete_opportunity(oid)
    return RedirectResponse("/opportunities?deleted=1", status_code=303)


@app.post("/opportunities/{oid}/evaluate")
def reevaluate(oid: int):
    get_op_or_404(oid)
    ok = pipeline.evaluate_opportunity(oid)
    return RedirectResponse(f"/opportunities/{oid}?notice={'evaluated' if ok else 'laya_down'}", status_code=303)


@app.post("/opportunities/{oid}/labels")
def save_labels(oid: int, type: str = Form(""), work_mode: str = Form(""), relevant: str = Form("")):
    get_op_or_404(oid)
    profile = profile_or_empty()
    labels = {}
    if type:
        if type not in criteria(profile, "type"):
            raise HTTPException(400, "Unknown type")
        labels["type"] = type
    if work_mode:
        if work_mode not in criteria(profile, "work_mode"):
            raise HTTPException(400, "Unknown work mode")
        labels["work_mode"] = work_mode
    if relevant in ("yes", "no"):
        labels["relevant"] = relevant == "yes"
    db.set_labels(oid, labels)
    return RedirectResponse(f"/opportunities/{oid}?notice=labels", status_code=303)


@app.get("/labels.jsonl")
def export_labels():
    """Labeled opportunities in eval/labels.jsonl format (see eval/run_eval.py)."""
    lines = []
    for op in db.labeled_opportunities():
        row = {k: op[k] for k in ("title", "company", "location") if op.get(k)}
        row["text"] = op["description"] or op["raw_text"]
        row.update(op["labels"])
        lines.append(json.dumps(row, ensure_ascii=False))
    return Response("\n".join(lines) + ("\n" if lines else ""), media_type="application/x-ndjson",
                    headers={"Content-Disposition": "attachment; filename=labels.jsonl"})


# --- drafts ---------------------------------------------------------------------

@app.get("/opportunities/{oid}/draft/{kind}", response_class=HTMLResponse)
def new_draft(oid: int, kind: str):
    op = get_op_or_404(oid)
    if kind not in DRAFT_KINDS:
        raise HTTPException(404, "Unknown draft type")
    return render("draft.html", o=op, kind=kind, draft=None, saved="", content=drafts.render(op, kind),
                  ai_model=ollama_client.MODEL)


@app.post("/opportunities/{oid}/ai-draft/{kind}")
def create_ai_draft(oid: int, kind: str):
    """Ask Ollama for a draft, save it, and open it for editing. Nothing is sent anywhere."""
    op = get_op_or_404(oid)
    if kind not in DRAFT_KINDS:
        raise HTTPException(404, "Unknown draft type")
    try:
        text = ollama_client.generate_draft(op, profile_or_empty(), kind, resume=pipeline.load_resume())
    except ollama_client.OllamaError as e:
        return detail_page(oid, f"No AI draft was created. {e}", notice_error=True, status_code=503)
    did = db.save_draft(oid, kind, text, status="ai_generated")
    return RedirectResponse(f"/drafts/{did}?saved=ai", status_code=303)


@app.post("/opportunities/{oid}/draft/{kind}")
def create_draft(oid: int, kind: str, content: str = Form(...)):
    get_op_or_404(oid)
    if kind not in DRAFT_KINDS:
        raise HTTPException(404, "Unknown draft type")
    did = db.save_draft(oid, kind, content)
    return RedirectResponse(f"/drafts/{did}?saved=1", status_code=303)


# Declared before /drafts/{did} so "12.md" is not taken for a draft id.
@app.get("/drafts/{did}.md")
def download_draft(did: int):
    d = db.get_draft(did)
    if d is None:
        raise HTTPException(404, "No such draft")
    return PlainTextResponse(d["content"], media_type="text/markdown; charset=utf-8",
                             headers={"Content-Disposition": f"attachment; filename=draft-{did}.md"})


@app.get("/drafts/{did}", response_class=HTMLResponse)
def edit_draft(did: int, saved: str = ""):
    d = db.get_draft(did)
    if d is None:
        raise HTTPException(404, "No such draft")
    op = get_op_or_404(d["opportunity_id"])
    return render("draft.html", o=op, kind=d["kind"], draft=d, saved=saved, content=d["content"],
                  ai_model=ollama_client.MODEL)


@app.post("/drafts/{did}")
def update_draft(did: int, content: str = Form(...)):
    d = db.get_draft(did)
    if d is None:
        raise HTTPException(404, "No such draft")
    db.save_draft(d["opportunity_id"], d["kind"], content, draft_id=did)
    return RedirectResponse(f"/drafts/{did}?saved=1", status_code=303)


# --- profile --------------------------------------------------------------------

def _csv(value: str) -> list[str]:
    return [v.strip() for v in value.split(",") if v.strip()]


RESUME_NOTICES = {"removed": "Your resume text was deleted from this PC."}


def render_profile(p: dict, status_code: int = 200, **context) -> HTMLResponse:
    context.setdefault("saved", False)
    context.setdefault("error", None)
    context.setdefault("notice", None)
    info = None
    if pipeline.load_resume():
        stored = db.get_resume()
        info = {"words": len(stored["text"].split()), "updated": (stored["updated_at"] or "")[:16]}
    return render("profile.html", status_code=status_code, p=_profile_view(p), resume=info,
                  employment_types=EMPLOYMENT_TYPES, ai_model=ollama_client.MODEL, **context)


@app.get("/profile", response_class=HTMLResponse)
def profile_form(saved: str = "", resume_notice: str = ""):
    return render_profile(profile_or_empty(), saved=bool(saved), notice=RESUME_NOTICES.get(resume_notice))


def _profile_view(p: dict) -> dict:
    view = {"name": "", "headline": "", "experience": "", "skills": [], "services": [], "work_modes": [],
            "employment_types": [], "preferred_locations": [],
            "excluded_locations": [], "min_salary": None, "currency": "USD", "confidence_threshold": 0.6,
            "notify_score": crawl.DEFAULT_NOTIFY_SCORE,
            "blocked_words": []}
    view.update({k: v for k, v in p.items() if k in view and v is not None})
    return view


def _merge(existing: list[str], new: list[str]) -> list[str]:
    seen = {v.lower() for v in existing}
    return existing + [v for v in new if v.lower() not in seen and not seen.add(v.lower())]


@app.post("/profile/resume", response_class=HTMLResponse)
async def upload_resume(file: UploadFile = File(...)):
    """Read the resume, keep its text in the database, and show the profile form filled in from it.
    The saved profile doesn't change until the user reviews the form and clicks Save."""
    current = profile_or_empty()
    data = await file.read(resume.MAX_BYTES + 1)
    try:
        text = resume.extract_text(file.filename or "", data)
    except resume.ResumeError as e:
        return render_profile(current, status_code=422, error=f"The resume wasn't read: {e}")
    db.save_resume(text)
    try:
        found = ollama_client.analyze_resume(text)
    except ollama_client.OllamaError as e:
        return render_profile(current, status_code=503,
                              error=f"Your resume text was saved (AI drafts will use it), but the profile wasn't "
                                    f"filled in from it. {e}")
    suggested = dict(current)
    for key in ("name", "headline", "experience"):
        if found[key]:
            suggested[key] = found[key]
    suggested["skills"] = _merge(list(current.get("skills") or []), found["skills"])
    suggested["services"] = _merge(list(current.get("services") or []), found["services"])
    if found["location"]:
        suggested["preferred_locations"] = _merge(list(current.get("preferred_locations") or []), [found["location"]])
    filled = [label for key, label in (("name", "name"), ("headline", "headline"), ("experience", "experience"),
                                       ("skills", "skills"), ("services", "services"), ("location", "location"))
              if found[key]]
    return render_profile(suggested, notice=(
        f"Filled in from your resume ({', '.join(filled) or 'nothing found'}). Review every field, set your "
        "preferences below, then click Save profile. Nothing is saved until you do."), from_resume=True)


@app.post("/profile/resume/delete")
def delete_resume():
    pipeline.load_resume()  # imports an old data/resume.txt first, so it can't come back after the delete
    db.delete_resume()
    return RedirectResponse("/profile?resume_notice=removed", status_code=303)


@app.post("/profile")
def save_profile(name: str = Form(""), headline: str = Form(""), experience: str = Form(""),
                 skills: str = Form(""), services: str = Form(""),
                 work_modes: list[str] = Form([]), employment_types: list[str] = Form([]),
                 preferred_locations: str = Form(""),
                 excluded_locations: str = Form(""), min_salary: str = Form(""), currency: str = Form(""),
                 confidence_threshold: str = Form("0.6"), blocked_words: str = Form(""),
                 notify_score: str = Form(str(crawl.DEFAULT_NOTIFY_SCORE))):
    try:
        profile = pipeline.load_profile()
    except (OSError, ValueError) as e:
        return render_profile({}, status_code=500, error=f"The saved profile can't be read, so it was not changed: {e}")
    updates = {"name": name.strip(), "headline": headline.strip(), "experience": experience.strip(),
               "skills": _csv(skills), "services": _csv(services),
               "work_modes": [m for m in work_modes if m in ("remote", "hybrid", "onsite")],
               "employment_types": [t for t in EMPLOYMENT_TYPES if t in employment_types],
               "preferred_locations": _csv(preferred_locations), "excluded_locations": _csv(excluded_locations),
               "blocked_words": _csv(blocked_words), "currency": currency.strip().upper() or "USD"}
    try:
        updates["min_salary"] = float(min_salary) if min_salary.strip() else None
        updates["confidence_threshold"] = float(confidence_threshold)
        updates["notify_score"] = int(notify_score)
        if not 0 <= updates["confidence_threshold"] <= 1 or not 0 <= updates["notify_score"] <= 100:
            raise ValueError
    except ValueError:
        return render_profile({**profile, **updates}, status_code=422,
                              error="Minimum salary must be a number, the confidence threshold between 0 and 1, "
                                    "and the notify score a whole number from 0 to 100.")
    profile.update(updates)  # questions, weights, and other keys are kept as they are
    db.save_profile(profile)
    return RedirectResponse("/profile?saved=1", status_code=303)


def _write_atomically(path: Path, text: str) -> None:
    # Write a temp file and swap it in, so a crash mid-write can't leave the file half-written.
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


# --- crawler sources ------------------------------------------------------------


def _read_sources() -> list[dict]:
    if not crawl.SOURCES_PATH.exists():
        return []
    try:
        value = json.loads(crawl.SOURCES_PATH.read_text(encoding="utf-8"))
        return value if isinstance(value, list) else []
    except (OSError, ValueError):
        return []

PAUSE_AFTER = 30  # suggest pausing a source that found this many jobs without one shortlist


def _source_rows(sources: list[dict]) -> list[dict]:
    """Each sources.json entry with its health and results, best results first. `index` is its place in the file."""
    states, stats, today = db.source_states(), db.source_stats(), time.strftime("%Y-%m-%d")
    rows = []
    for index, s in enumerate(sources):
        key = f"{s.get('kind')}:{crawl.source_url(s)}"
        state, stat = states.get(key) or {}, stats.get(key) or {"found": 0, "shortlisted": 0}
        limit = state.get("runs_date") == today and state.get("runs_today", 0) >= (s.get("max_runs_per_day") or 3)
        rows.append({"index": index, "source": s, "state": state, "stats": stat, "limit_reached": limit,
                     "suggest_pause": not s.get("paused") and stat["found"] >= PAUSE_AFTER and not stat["shortlisted"]})
    return sorted(rows, key=lambda r: (-(r["stats"]["shortlisted"] or 0), -(r["stats"].get("avg_score") or 0)))


def failing_sources(sources: list[dict]) -> list[dict]:
    """Sources that failed two runs in a row or more, for the dashboard warning."""
    return [r for r in _source_rows(sources) if (r["state"].get("error_count") or 0) >= 2]


@app.get("/sources", response_class=HTMLResponse)
def sources_page(saved: str = "", error: str = "", notice: str = ""):
    sources = _read_sources()
    configured = {f"{s.get('kind')}:{crawl.source_url(s)}" for s in sources}
    suggestions = [x for x in db.list_suggestions() if f"{x['kind']}:{x['url']}" not in configured]
    return render("sources.html", rows=_source_rows(sources), suggestions=suggestions, saved=bool(saved),
                  error=error, notice=notice, pause_after=PAUSE_AFTER)


def _search_terms(sources: list[dict]) -> list[str]:
    """Search words the configured sources already use (OnlineJobs.ph jobkeyword=, Remotive search=)."""
    terms = []
    for s in sources:
        query = parse_qs(urlsplit(str(s.get("url") or "")).query)
        terms += [t for key in ("jobkeyword", "search") for t in query.get(key, [])]
    return terms


@app.post("/sources/suggest-keywords")
def suggest_keywords():
    shortlisted = db.list_opportunities(status="shortlisted")
    if not shortlisted:
        return sources_page(error="Shortlist a few jobs first: keyword suggestions come from what you shortlist.")
    # Contract with keywords.py and ollama_client.suggest_keywords: existing terms go in profile["sources"].
    profile = {**profile_or_empty(), "sources": _search_terms(_read_sources())}
    try:
        terms, note = ollama_client.suggest_keywords(shortlisted, profile), ""
    except ollama_client.OllamaError:
        terms, note = keywords.suggest_keywords(shortlisted, profile), " Ollama didn't answer, so these come from word counts."
    added = db.add_suggestions(keywords.search_sources(terms), "keywords")
    return sources_page(notice=f"{added} new suggestion{'' if added == 1 else 's'} from {len(shortlisted)} "
                               f"shortlisted job{'' if len(shortlisted) == 1 else 's'}.{note}")


@app.post("/sources/suggestions/{sid}/{action}")
def act_on_suggestion(sid: int, action: str):
    suggestion = db.get_suggestion(sid)
    if suggestion is None or action not in ("add", "dismiss"):
        raise HTTPException(404, "No such suggestion")
    if action == "add":
        if suggestion["kind"] not in crawl.KINDS:
            return sources_page(error=f"The crawler can't read {suggestion['kind']} sources yet.")
        sources = _read_sources()
        sources.append({"kind": suggestion["kind"], "url": suggestion["url"], "name": suggestion["name"] or "",
                        "max_runs_per_day": crawl.DEFAULT_RUNS_PER_DAY})
        _write_atomically(crawl.SOURCES_PATH, json.dumps(sources, indent=2, ensure_ascii=False) + "\n")
    db.set_suggestion_status(sid, "added" if action == "add" else "dismissed")
    return RedirectResponse("/sources?saved=1", status_code=303)

@app.post("/sources")
def save_source(kind: str = Form(...), url: str = Form(""), board: str = Form(""), name: str = Form(""),
                max_runs_per_day: str = Form("3"), thread: str = Form(""), label: str = Form("")):
    try:
        runs = int(max_runs_per_day)
        if runs < 1: raise ValueError
    except ValueError:
        return sources_page(error="Runs per day must be a positive whole number.")
    kind = kind.strip().lower(); source = {"kind": kind, "name": name.strip(), "max_runs_per_day": runs}
    if kind in {"greenhouse", "lever"}:
        if not board.strip(): return sources_page(error="A board name is required for Greenhouse or Lever.")
        source["board"] = board.strip()
    elif kind in {"rss", "page", "remotive", "remoteok"}:
        if not url.startswith(("http://", "https://")): return sources_page(error="URL must start with http:// or https://.")
        source["url"] = url.strip()
        if kind == "remotive":
            source["max_runs_per_day"] = min(runs, crawl.REMOTIVE_MAX_RUNS)
    elif kind == "hn":
        if thread not in crawl.HN_THREADS: return sources_page(error="Pick a Hacker News thread.")
        source["thread"] = thread
    elif kind == "imap":
        if not label.strip(): return sources_page(error="Enter the mail label that holds your job alerts.")
        source["label"] = label.strip()
    else:
        return sources_page(error="Unknown source type.")
    sources = _read_sources(); sources.append(source)
    _write_atomically(crawl.SOURCES_PATH, json.dumps(sources, indent=2, ensure_ascii=False) + "\n")
    return RedirectResponse("/sources?saved=1", status_code=303)

@app.post("/sources/{index}/pause")
def toggle_pause(index: int):
    sources = _read_sources()
    if index < 0 or index >= len(sources): raise HTTPException(404, "No such source")
    if sources[index].pop("paused", False) is False:
        sources[index]["paused"] = True
    _write_atomically(crawl.SOURCES_PATH, json.dumps(sources, indent=2, ensure_ascii=False) + "\n")
    return RedirectResponse("/sources?saved=1", status_code=303)

@app.post("/sources/{index}/delete")
def delete_source(index: int):
    sources = _read_sources()
    if index < 0 or index >= len(sources): raise HTTPException(404, "No such source")
    sources.pop(index)
    _write_atomically(crawl.SOURCES_PATH, json.dumps(sources, indent=2, ensure_ascii=False) + "\n")
    return RedirectResponse("/sources?saved=1", status_code=303)
