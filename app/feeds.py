"""Pure parsers for public job and hiring feeds.

These functions do not perform network requests.  The crawler owns fetching and
passes decoded JSON or Algolia records here.
"""
from __future__ import annotations

import re
from html import unescape

from .extract import html_to_text, guess_remote
from .normalize import canonical_url


def _text(value) -> str:
    return html_to_text(unescape(str(value or ""))).strip()


def remotive_items(source: dict, data) -> list[tuple[str, dict]]:
    rows = data.get("jobs", []) if isinstance(data, dict) else data or []
    out = []
    for job in rows:
        if not isinstance(job, dict):
            continue
        url = job.get("url") or job.get("job_url")
        item_id = str(job.get("id") or canonical_url(url) or "")
        if not item_id:
            continue
        description = _text(job.get("description") or job.get("description_plain"))
        out.append((item_id, {"source_url": url, "title": _text(job.get("title")),
                              "company": _text(job.get("company_name") or job.get("company")),
                              "description": description, "location": _text(job.get("candidate_required_location")),
                              "remote": True if job.get("remote") is True else guess_remote(description),
                              "salary_min": job.get("salary_min"), "salary_max": job.get("salary_max"),
                              "currency": job.get("salary_currency"), "published_at": job.get("publication_date"),
                              "warnings": []}))
    return out


def remoteok_items(source: dict, data) -> list[tuple[str, dict]]:
    rows = (data[1:] if isinstance(data, list) else [])
    out = []
    for job in rows:
        if not isinstance(job, dict):
            continue
        url = job.get("url") or job.get("apply_url")
        item_id = str(job.get("id") or canonical_url(url) or "")
        if not item_id:
            continue
        description = _text(job.get("description"))
        out.append((item_id, {"source_url": url, "title": _text(job.get("position") or job.get("title")),
                              "company": _text(job.get("company")), "description": description,
                              "location": _text(job.get("location")), "remote": True,
                              "salary_min": job.get("salary_min"), "salary_max": job.get("salary_max"),
                              "currency": job.get("salary_currency"), "published_at": job.get("date"),
                              "warnings": []}))
    return out


def hn_items(source: dict, data) -> list[tuple[str, dict]]:
    hits = data.get("hits", []) if isinstance(data, dict) else data or []
    out = []
    for hit in hits:
        if not isinstance(hit, dict):
            continue
        text = _text(hit.get("comment_text") or hit.get("story_text"))
        if not text:
            continue
        first = text.splitlines()[0].strip()
        parts = [p.strip() for p in first.split("|")]
        company = title = location = None
        if len(parts) >= 2:
            company, title = parts[0], parts[1]
            location = parts[2] if len(parts) >= 3 else None
            remote = "remote" in " ".join(parts[2:]).lower()
        else:
            title, remote = first[:160], guess_remote(text)
        item_id = str(hit.get("objectID") or hit.get("story_id") or "")
        if not item_id:
            continue
        url = hit.get("url") or (f"https://news.ycombinator.com/item?id={item_id}")
        out.append((item_id, {"source_url": url, "title": title, "company": company,
                              "description": text, "location": location, "remote": remote,
                              "published_at": hit.get("created_at"), "warnings": []}))
    return out


def is_hiring_post(title: str) -> bool:
    """Return true for Reddit hiring posts, excluding For Hire posts."""
    title = title or ""
    return bool(re.search(r"\[\s*hiring\s*\]", title, re.I)) and not bool(
        re.search(r"\[\s*for\s+hire\s*\]", title, re.I))
