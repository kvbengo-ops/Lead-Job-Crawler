"""Parse job-alert emails into the crawler's normalized item contract."""
from __future__ import annotations

import email
import re
from email.message import EmailMessage
from html import unescape
from urllib.parse import parse_qs, unquote, urlsplit

from .extract import html_to_text
from .normalize import canonical_url

URL_RE = re.compile(r'https?://[^\s<>"\']+', re.I)
TRACK_KEYS = ("url", "u", "redirect", "redirect_url", "dest", "destination", "target")


def _urls(text: str) -> list[str]:
    found = []
    for raw in URL_RE.findall(text or ""):
        raw = raw.rstrip(".,);]")
        query = parse_qs(urlsplit(raw).query)
        target = next((query[k][0] for k in TRACK_KEYS if query.get(k)), raw)
        target = unquote(target)
        if urlsplit(target).scheme in ("http", "https"):
            target = canonical_url(target) or target
            if target not in found:
                found.append(target)
    return found


def _body(message: EmailMessage) -> str:
    chunks = []
    for part in message.walk() if message.is_multipart() else [message]:
        if part.get_content_maintype() == "multipart":
            continue
        content = part.get_content()
        if part.get_content_type() == "text/html":
            content = html_to_text(unescape(content))
        if part.get_content_type() in ("text/plain", "text/html"):
            chunks.append(content)
    return "\n".join(chunks)


def _fields(url: str, body: str, sender: str) -> dict:
    lines = [re.sub(r"\s+", " ", x).strip() for x in body.splitlines() if x.strip()]
    title = next((x for x in lines if len(x) > 5 and "http" not in x.lower()), "Job alert")
    company = None
    location = None
    for line in lines:
        if re.search(r"(?:company|employer)\s*:", line, re.I):
            company = line.split(":", 1)[1].strip()
        if re.search(r"location\s*:", line, re.I):
            location = line.split(":", 1)[1].strip()
    return {"source_url": url, "title": title[:240], "company": company,
            "description": body[:10000], "location": location,
            "remote": bool(re.search(r"\bremote\b", body, re.I)), "warnings": []}


def alert_items(message: EmailMessage) -> list[tuple[str, dict]]:
    """Return one item per canonical posting URL; non-alert mail returns no items."""
    sender = (message.get("From") or "").lower()
    subject = message.get("Subject") or ""
    body = _body(message)
    if not re.search(r"job|hiring|vacanc|opportunit|freelance|career", sender + " " + subject + " " + body, re.I):
        return []
    out = []
    for url in _urls(body):
        out.append((canonical_url(url) or url, _fields(url, body, sender)))
    return out
