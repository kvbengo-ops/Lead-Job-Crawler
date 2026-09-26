"""Markdown draft templates. Drafts are only saved and downloaded; nothing is ever sent."""
from __future__ import annotations

from . import pipeline

EXCERPT_CHARS = 800


def _profile() -> dict:
    try:
        return pipeline.load_profile()
    except (OSError, ValueError):
        return {}


def render(op: dict, kind: str) -> str:
    p = _profile()
    name = p.get("name") or "[Your name]"
    skills = ", ".join(p.get("skills", []) + p.get("services", [])) or "[your relevant skills]"
    title = op.get("title") or "this opportunity"
    company = op.get("company") or "your team"
    if kind == "lead_email":
        return (f"Subject: A possible way to help {company}\n\n"
                f"Hello,\n\n"
                f"I came across {title}. Based on what you described, I may be able to help with {skills}.\n\n"
                f"[One or two sentences on a specific result you could deliver for them.]\n\n"
                f"Would a short call next week be useful?\n\n"
                f"Best regards,\n{name}\n")
    description = (op.get("description") or "").strip()
    if len(description) > EXCERPT_CHARS:
        description = description[:EXCERPT_CHARS].rsplit(" ", 1)[0] + " …"
    quoted = "\n".join("> " + line if line else ">" for line in description.splitlines()) or "> [paste the key requirements]"
    return (f"Subject: Application for {title}\n\n"
            f"Hello,\n\n"
            f"I'd like to apply for {title} at {company}. My relevant experience includes {skills}.\n\n"
            f"[Two or three sentences matching your experience to the requirements below.]\n\n"
            f"Requirements from the posting:\n\n{quoted}\n\n"
            f"I would welcome the chance to discuss how I can contribute.\n\n"
            f"Best regards,\n{name}\n")
