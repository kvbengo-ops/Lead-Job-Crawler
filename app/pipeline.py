"""The one path every opportunity takes: extract -> normalize -> dedupe -> Laya -> score -> store.
Used by the dashboard and the crawler alike, so they cannot drift apart."""
from __future__ import annotations

import json

from . import db
from .extract import from_text, from_url
from .laya_client import evaluate
from .normalize import canonical_url, normalize
from .score import score

# The profile and resume live in the database. These two files are where older versions kept them; each is
# imported once, then renamed to *.imported so it is neither read again nor lost.
PROFILE_PATH = db.ROOT / "profile.json"
RESUME_PATH = db.ROOT / "data" / "resume.txt"
EXAMPLE_PROFILE_PATH = db.ROOT / "profile.example.json"  # shared template, used until a profile is saved


def _import_file(path, save) -> None:
    if path.exists():
        # utf-8-sig: Windows editors often save a BOM, which plain utf-8 json parsing rejects.
        save(path.read_text(encoding="utf-8-sig"))  # a bad file raises here and stays where it is
        path.replace(path.with_name(path.name + ".imported"))


def load_profile() -> dict:
    if db.get_profile() is None:
        _import_file(PROFILE_PATH, lambda text: db.save_profile(json.loads(text)))
    profile = db.get_profile()
    return profile if profile is not None else json.loads(EXAMPLE_PROFILE_PATH.read_text(encoding="utf-8-sig"))


def load_resume() -> str:
    if db.get_resume() is None:
        _import_file(RESUME_PATH, db.save_resume)
    resume = db.get_resume()
    return resume["text"] if resume else ""


def process(text: str | None = None, url: str | None = None, op: dict | None = None) -> tuple[int, bool]:
    """Store one opportunity and evaluate it unless it is a duplicate. Returns (id, is_duplicate); for a URL
    that is already stored, that is the original's id and nothing is fetched or stored.
    Raises extract.FetchError when a URL can't be fetched, ValueError when there is nothing to process."""
    if op is not None:
        record = normalize(op)
    elif url and url.strip():
        original = db.find_original(canonical_url(url.strip()))
        if original is not None:
            return original, True
        record = from_url(url.strip())
    elif text and text.strip():
        record = from_text(text)
    else:
        raise ValueError("Nothing to add: paste some text or a URL.")
    oid, duplicate = db.insert_opportunity(record)
    if not duplicate:
        evaluate_opportunity(oid)
    return oid, duplicate


def evaluate_opportunity(oid: int, profile: dict | None = None) -> bool:
    """Ask Laya and score. Returns False when Laya failed; a new item is then marked pending_evaluation."""
    op = db.get_opportunity(oid)
    profile = load_profile() if profile is None else profile
    evaluation = evaluate(op, profile)
    if evaluation["error"]:
        db.save_evaluation(oid, evaluation, None)
        if op["status"] == "new":
            db.set_status(oid, "pending_evaluation")
        return False
    db.save_evaluation(oid, evaluation, score(op, evaluation, profile))
    if op["status"] == "pending_evaluation":
        db.set_status(oid, "new")
    return True


def retry_pending(profile: dict | None = None) -> tuple[int, int]:
    """Re-evaluate pending items. Stops at the first failure so a down Laya isn't called once per item.
    Returns (evaluated, pending before)."""
    ids = db.pending_ids()
    profile = load_profile() if profile is None and ids else profile
    done = 0
    for oid in ids:
        if not evaluate_opportunity(oid, profile):
            break
        done += 1
    return done, len(ids)
