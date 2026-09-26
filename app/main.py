"""Local dashboard: python -m uvicorn app.main:app --host 127.0.0.1 --port 8100 (scripts/start.ps1 does this)."""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse, Response
from jinja2 import Environment, FileSystemLoader

from . import db, drafts, pipeline
from .extract import FetchError

app = FastAPI(title="Local Job and Lead Crawler")
db.init()
# autoescape: titles and descriptions come from scraped pages and must never be rendered as HTML.
templates = Environment(loader=FileSystemLoader(Path(__file__).parent / "templates"), autoescape=True)

STATUSES = ["new", "shortlisted", "ignored", "pending_evaluation"]
USER_STATUSES = {"new", "shortlisted", "ignored"}
DRAFT_KINDS = {"job_application", "lead_email"}


def render(name: str, status_code: int = 200, **context) -> HTMLResponse:
    return HTMLResponse(templates.get_template(name).render(**context), status_code=status_code)


def profile_or_empty() -> dict:
    try:
        return pipeline.load_profile()
    except (OSError, ValueError):
        return {}


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
def home(status: str = "", kind: str = "", review: str = "", rejected: str = ""):
    previous_visit = db.swap_timestamp("last_visit")
    rows = db.list_opportunities(status=status if status in STATUSES else None, kind=kind or None,
                                 needs_review=bool(review), show_rejected=bool(rejected))
    return render("list.html", rows=rows, statuses=STATUSES, kinds=criteria(profile_or_empty(), "type"),
                  f={"status": status, "kind": kind, "review": review, "rejected": rejected},
                  last_run=db.last_run(), new_count=db.count_created_since(previous_visit) if previous_visit else 0)


@app.get("/add", response_class=HTMLResponse)
def add_form():
    return render("add.html", url="", text="", error=None)


@app.post("/add")
def add(url: str = Form(""), text: str = Form("")):
    try:
        oid, duplicate = pipeline.process(text=text, url=url or None)
    except (FetchError, ValueError) as e:
        return render("add.html", status_code=422, url=url, text=text, error=str(e))
    if duplicate:
        original = db.get_opportunity(oid)["duplicate_of"] or oid
        return RedirectResponse(f"/opportunities/{original}?notice=duplicate", status_code=303)
    return RedirectResponse(f"/opportunities/{oid}", status_code=303)


NOTICES = {
    "duplicate": ("This URL is already in your list; here is the original.", False),
    "laya_down": ("Laya didn't answer, so this is marked pending and will be retried on the next crawl.", True),
    "evaluated": ("Re-evaluated with Laya.", False),
    "labels": ("Labels saved.", False),
}


@app.get("/opportunities/{oid}", response_class=HTMLResponse)
def detail(oid: int, notice: str = ""):
    op = get_op_or_404(oid)
    ev = db.latest_evaluation(oid)
    profile = profile_or_empty()
    link = op["source_url"] if op["source_url"] and urlsplit(op["source_url"]).scheme in ("http", "https") else None
    text, is_error = NOTICES.get(notice, (None, False))
    return render("detail.html", o=op, ev=ev, answers=(ev or {}).get("result", {}).get("answers") or {},
                  weights=profile.get("score_weights") or {}, threshold=profile.get("confidence_threshold", 0.6),
                  type_options=criteria(profile, "type"), work_mode_options=criteria(profile, "work_mode"),
                  drafts=db.list_drafts(oid), source_link=link, notice=text, notice_error=is_error)


@app.post("/opportunities/{oid}/status")
def set_status(oid: int, status: str = Form(...)):
    get_op_or_404(oid)
    if status not in USER_STATUSES:
        raise HTTPException(400, "Unknown status")
    db.set_status(oid, status)
    return RedirectResponse(f"/opportunities/{oid}", status_code=303)


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
    return render("draft.html", o=op, kind=kind, draft=None, saved=False, content=drafts.render(op, kind))


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
    return render("draft.html", o=op, kind=d["kind"], draft=d, saved=bool(saved), content=d["content"])


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


@app.get("/profile", response_class=HTMLResponse)
def profile_form(saved: str = ""):
    return render("profile.html", p=_profile_view(profile_or_empty()), saved=bool(saved), error=None)


def _profile_view(p: dict) -> dict:
    view = {"name": "", "headline": "", "skills": [], "services": [], "work_modes": [], "preferred_locations": [],
            "excluded_locations": [], "min_salary": None, "currency": "USD", "confidence_threshold": 0.6,
            "blocked_words": []}
    view.update({k: v for k, v in p.items() if k in view and v is not None})
    return view


@app.post("/profile")
def save_profile(name: str = Form(""), headline: str = Form(""), skills: str = Form(""), services: str = Form(""),
                 work_modes: list[str] = Form([]), preferred_locations: str = Form(""),
                 excluded_locations: str = Form(""), min_salary: str = Form(""), currency: str = Form(""),
                 confidence_threshold: str = Form("0.6"), blocked_words: str = Form("")):
    try:
        profile = pipeline.load_profile()
    except (OSError, ValueError) as e:
        return render("profile.html", status_code=500, p=_profile_view({}), saved=False,
                      error=f"profile.json can't be read, so it was not changed: {e}")
    updates = {"name": name.strip(), "headline": headline.strip(), "skills": _csv(skills), "services": _csv(services),
               "work_modes": [m for m in work_modes if m in ("remote", "hybrid", "onsite")],
               "preferred_locations": _csv(preferred_locations), "excluded_locations": _csv(excluded_locations),
               "blocked_words": _csv(blocked_words), "currency": currency.strip().upper() or "USD"}
    try:
        updates["min_salary"] = float(min_salary) if min_salary.strip() else None
        updates["confidence_threshold"] = float(confidence_threshold)
        if not 0 <= updates["confidence_threshold"] <= 1:
            raise ValueError
    except ValueError:
        return render("profile.html", status_code=422, p=_profile_view({**profile, **updates}), saved=False,
                      error="Minimum salary must be a number, and the confidence threshold between 0 and 1.")
    profile.update(updates)  # questions, weights, and other keys are kept as they are
    _write_atomically(pipeline.PROFILE_PATH, json.dumps(profile, indent=2, ensure_ascii=False) + "\n")
    return RedirectResponse("/profile?saved=1", status_code=303)


def _write_atomically(path: Path, text: str) -> None:
    # Write a temp file and swap it in, so a crash mid-write can't leave profile.json half-written.
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
