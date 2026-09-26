"""Hard filters and the Phase 1 fit score (see PLAN.md, "Filter and scoring engine").

Each component is 0..1; the score is their weighted average (profile "score_weights") scaled to 0..100.
Unknown facts score 0.5 (neutral) rather than 0, so missing data never looks like a bad match.
"""
import re


def _mentions(term: str, text: str) -> bool:
    # Word-boundary match that also works for terms like "c++" or "node.js", and avoids "india" in "indianapolis".
    return re.search(r"(?<!\w)" + re.escape(term.lower()) + r"(?!\w)", text) is not None


def _answer(evaluation: dict, name: str, threshold: float):
    """(value, confident) for a Laya answer, or (None, False) when it is missing."""
    a = (evaluation.get("answers") or {}).get(name)
    if not a:
        return None, False
    return a["value"], a["answer_confidence"] >= threshold


def score(opportunity: dict, evaluation: dict, profile: dict) -> dict:
    threshold = profile.get("confidence_threshold", 0.6)
    text = " ".join(str(opportunity.get(k) or "") for k in ("title", "company", "description")).lower()
    location = (opportunity.get("location") or "").lower()
    reject, reasons, components = [], [], {}
    needs_review = False

    # --- review flags -------------------------------------------------------
    if evaluation.get("error"):
        needs_review = True
        reasons.append(f"Laya evaluation failed: {evaluation['error']}")
    for name, a in (evaluation.get("answers") or {}).items():
        if a["answer_confidence"] < threshold:
            needs_review = True
            reasons.append(f"Low Laya confidence on {name}: {a['answer_confidence']:.0%} < {threshold:.0%}")

    # --- hard filters -------------------------------------------------------
    if opportunity.get("duplicate_of") is not None:
        reject.append(f"Duplicate of #{opportunity['duplicate_of']}")
    kind, kind_confident = _answer(evaluation, "type", threshold)
    if kind in profile.get("reject_types", []):
        if kind_confident:
            reject.append(f"Laya type is {kind}")
        else:
            reasons.append(f"Laya suggests {kind}, but not confidently; kept for review")
    for word in profile.get("blocked_words", []):
        if _mentions(word, text):
            reject.append(f"Blocked word: {word}")
    for place in profile.get("excluded_locations", []):
        if _mentions(place, location):
            reject.append(f"Excluded location: {place}")

    # --- salary ---------------------------------------------------------------
    minimum, want_cur = profile.get("min_salary"), (profile.get("currency") or "").upper()
    top = opportunity.get("salary_max") if opportunity.get("salary_max") is not None else opportunity.get("salary_min")
    cur = (opportunity.get("currency") or "").upper()
    if minimum is None:
        components["salary"] = 1.0
        reasons.append("No minimum salary set")
    elif top is None:
        components["salary"] = 0.5
        reasons.append("Salary not stated")
    elif cur and want_cur and cur != want_cur:
        components["salary"] = 0.5
        reasons.append(f"Salary in {cur}, not compared with your {want_cur} minimum")
    elif top >= minimum:
        components["salary"] = 1.0
        reasons.append(f"Salary up to {top:g} meets your minimum of {minimum:g}")
    else:
        components["salary"] = 0.0
        reject.append(f"Salary up to {top:g} is below your minimum of {minimum:g}")

    # --- relevance (Laya) -----------------------------------------------------
    relevance, _ = _answer(evaluation, "relevance", threshold)
    if relevance is None:
        components["relevance"] = 0.5
        reasons.append("No Laya relevance answer")
    else:
        components["relevance"] = float(relevance)
        reasons.append(f"Laya relevance {relevance:.0%}")

    # --- skills ---------------------------------------------------------------
    terms = profile.get("skills", []) + profile.get("services", [])
    matched = [t for t in terms if _mentions(t, text)]
    if not terms:
        components["skills"] = 0.5
        reasons.append("No skills in profile")
    else:
        components["skills"] = min(1.0, len(matched) / max(1, profile.get("skills_for_full_match", 3)))
        reasons.append(f"Skills matched: {', '.join(matched)}" if matched else "No profile skills mentioned")

    # --- location and work mode ----------------------------------------------
    # remote is only a signal when True: the extractor sets False when "remote" is simply not mentioned.
    if opportunity.get("remote") is True:
        mode = "remote"
    else:
        laya_mode, mode_confident = _answer(evaluation, "work_mode", threshold)
        mode = laya_mode if mode_confident and laya_mode != "unclear" else None
    modes = profile.get("work_modes", [])
    preferred = [p for p in profile.get("preferred_locations", []) if _mentions(p, location)]
    if preferred:
        components["location"] = 1.0
        reasons.append(f"Preferred location: {', '.join(preferred)}")
    elif mode is None or not modes:
        components["location"] = 0.5
        reasons.append("Work mode unclear" if mode is None else f"Work mode {mode}; no preference set")
    elif mode in modes:
        components["location"] = 1.0
        reasons.append(f"Work mode {mode} matches your preference")
    else:
        components["location"] = 0.0
        reasons.append(f"Work mode {mode} is not in your preferences ({', '.join(modes)})")

    weights = profile.get("score_weights") or {"relevance": 60, "skills": 20, "salary": 10, "location": 10}
    total = sum(weights.get(k, 0) for k in components)
    value = sum(components[k] * weights.get(k, 0) for k in components) / total * 100 if total else 0.0
    return {
        "passed": not reject,
        "reject_reasons": reject,
        "score": round(value, 1),
        "components": {k: round(v, 3) for k, v in components.items()},
        "reasons": reasons,
        "needs_review": needs_review,
    }
