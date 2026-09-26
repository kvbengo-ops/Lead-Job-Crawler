"""The one path every opportunity takes: extract -> normalize -> dedupe -> Laya -> score -> store.
Used by the dashboard and the crawler alike, so they cannot drift apart."""
from __future__ import annotations

import json

from . import db
from .extract import from_text, from_url
from .laya_client import evaluate
from .normalize import canonical_url, normalize
from .score import score

PROFILE_PATH = db.ROOT / "profile.json"


def load_profile() -> dict:
    # utf-8-sig: Windows editors often save a BOM, which plain utf-8 json parsing rejects.
    return json.loads(PROFILE_PATH.read_text(encoding="utf-8-sig"))


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
