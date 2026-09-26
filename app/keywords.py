"""Derive search terms and ready-made source suggestions from shortlisted jobs."""
from __future__ import annotations

import re


def _words(text: str) -> list[str]:
    return re.findall(r"[A-Za-z][A-Za-z0-9+#.-]{2,}", text or "")


def search_sources(keywords: list[str]) -> list[dict]:
    out = []
    for keyword in keywords:
        q = keyword.strip()
        if not q:
            continue
        encoded = q.replace(" ", "+")
        out.append({"kind": "page", "url": f"https://www.onlinejobs.ph/jobseekers/jobsearch?jobkeyword={encoded}",
                    "name": f"OnlineJobs.ph: {q}", "why": f"Searches OnlineJobs.ph for {q}."})
        out.append({"kind": "remotive", "url": f"https://remotive.com/api/remote-jobs?search={encoded}",
                    "name": f"Remotive: {q}", "why": f"Searches Remotive for {q}."})
    return out


def _existing(profile: dict) -> set[str]:
    values = profile.get("sources", []) if isinstance(profile, dict) else []
    return {str(x).lower() for x in values} if isinstance(values, list) else set()


def suggest_keywords(shortlisted: list[dict], profile: dict) -> list[str]:
    """Fallback keyword extraction; callers may replace this with Ollama suggestions."""
    counts: dict[str, int] = {}
    existing = _existing(profile)
    for job in shortlisted:
        text = " ".join(str(job.get(k) or "") for k in ("title", "description", "company"))
        for word in _words(text):
            low = word.lower()
            if low not in existing and low not in {"the", "and", "with", "for", "job", "work", "remote"}:
                counts[low] = counts.get(low, 0) + 1
    return [w for w, _ in sorted(counts.items(), key=lambda x: (-x[1], x[0]))[:10]]
