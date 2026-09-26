"""Clean and validate an extracted opportunity. Problems become warnings; nothing is silently dropped."""
from __future__ import annotations

import re
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from html import unescape
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

TRACKING_PARAMS = {"fbclid", "gclid", "msclkid", "mc_cid", "mc_eid", "ref", "ref_src", "_hsenc", "_hsmi", "igshid"}
EMAIL = re.compile(r"^[\w.+-]+@[\w-]+(\.[\w-]+)+$")

# Employment types, in the order the profile page shows them. "flexible_hours" can accompany any other type.
EMPLOYMENT_TYPES = ("full_time", "part_time", "contract", "freelance", "internship", "flexible_hours")
EMPLOYMENT_PATTERNS = {
    "full_time": r"\bfull[- ]?time\b",
    "part_time": r"\bpart[- ]?time\b",
    "contract": r"\b(?:contract(?:or)? (?:role|position|job|basis)|fixed[- ]term|temporary (?:role|position|contract)"
                r"|contract[- ]to[- ]hire|\d+[- ]month contract)\b",
    "freelance": r"\b(?:freelanc\w*|fixed[- ]price|hourly project|upwork|fiverr)\b",
    "internship": r"\bintern(?:ship)?s?\b",
    "flexible_hours": r"\bflexib(?:le|ility)(?: working)? (?:hours|schedule|working hours|work hours|time)\b|\bflexitime\b|\bflextime\b",
}
# Values sources use (schema.org employmentType, Lever "commitment") mapped to ours.
EMPLOYMENT_ALIASES = {
    "full_time": "full_time", "fulltime": "full_time", "full-time": "full_time", "full time": "full_time",
    "permanent": "full_time",
    "part_time": "part_time", "parttime": "part_time", "part-time": "part_time", "part time": "part_time",
    "contractor": "contract", "contract": "contract", "temporary": "contract", "temp": "contract",
    "fixed term": "contract", "per_diem": "contract",
    "freelance": "freelance", "freelancer": "freelance",
    "intern": "internship", "internship": "internship",
    "flexible_hours": "flexible_hours", "flexible hours": "flexible_hours",
}


def employment_types(given, text: str) -> list[str]:
    """Types the source states (schema.org or Lever values), else types the text mentions.
    Flexible hours are always looked for in the text, since sources don't encode them."""
    values = given if isinstance(given, list) else [given] if given else []
    found = {EMPLOYMENT_ALIASES.get(str(v).strip().lower()) for v in values} - {None}
    stated = bool(found)
    for key, pattern in EMPLOYMENT_PATTERNS.items():
        if (not stated or key == "flexible_hours") and re.search(pattern, text, re.I):
            found.add(key)
    return [t for t in EMPLOYMENT_TYPES if t in found]


def canonical_url(url: str | None) -> str | None:
    """Lowercase scheme and host, drop tracking parameters, the fragment, and a trailing slash."""
    if not url:
        return None
    p = urlsplit(url.strip())
    if not p.netloc:
        return url.strip()
    query = [(k, v) for k, v in parse_qsl(p.query, keep_blank_values=True)
             if not (k.lower().startswith("utm_") or k.lower() in TRACKING_PARAMS)]
    host = p.netloc.lower()
    host = host[:-3] if host.endswith(":80") and p.scheme.lower() == "http" else host
    host = host[:-4] if host.endswith(":443") and p.scheme.lower() == "https" else host
    return urlunsplit(((p.scheme or "https").lower(), host, p.path.rstrip("/") or "/", urlencode(query), ""))


def clean(value):
    if isinstance(value, str):
        return re.sub(r"\s+", " ", value).strip() or None
    return value


def clean_title(title: str | None, source_url: str | None) -> str | None:
    """Unescape leftover entities and drop a trailing " 1234567 - Board.com" when Board.com is the page's host.
    A plain name ("Data Engineer | Globex") stays: on a company's own site that is the company."""
    title = clean(unescape(title)) if title else None
    host = re.sub(r"\W", "", urlsplit(source_url or "").netloc.lower())
    m = re.match(r"^(.*\S)\s+[-|–—]\s+([^-|–—]+)$", title or "")
    if m and host and "." in m.group(2) and (site :=re.sub(r"\W", "", m.group(2).lower())) and site in host:
        return re.sub(r"\s+#?\d{4,}$", "", m.group(1))
    return title


# Job-board page chrome that says nothing about the job.
# ponytail: a phrase list; add phrases as new boards show up.
BOILERPLATE = re.compile(r"^(<|back to|please (log ?in|sign ?in|register)|sign in|apply now|share this)", re.I)


def strip_boilerplate(text: str, title: str | None = None) -> str:
    """The text without navigation lines, blank lines, or a line that only repeats the title."""
    title = (title or "").strip().lower()
    return "\n".join(line for line in (l.strip() for l in text.splitlines())
                     if line and not BOILERPLATE.match(line) and line.lower() != title)


def iso_date(value) -> str | None:
    """ISO 8601 (UTC when a time is given) from ISO strings, RSS/email dates, or epoch milliseconds."""
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value / 1000 if value > 1e11 else value, timezone.utc).isoformat()
    text = str(value).strip()
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        datetime.strptime(text, "%Y-%m-%d")  # raises ValueError for impossible dates
        return text
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        dt = parsedate_to_datetime(text)  # RFC 2822 (RSS); raises ValueError/TypeError when not a date
    return (dt.astimezone(timezone.utc) if dt.tzinfo else dt).isoformat()


def _number(value) -> float | None:
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return float(str(value).replace(",", "").strip())


def normalize(op: dict) -> dict:
    result = dict(op)
    warnings = list(result.get("warnings") or [])
    for key in ("title", "company", "location", "contact_email", "source_url", "currency"):
        result[key] = clean(result.get(key))
    result["title"] = clean_title(result["title"], result["source_url"])
    # Keep line breaks in the description (they carry structure); only tidy spaces within lines.
    description = result.get("description") or ""
    description = "\n".join(re.sub(r"[ \t\f\v]+", " ", line).strip() for line in description.splitlines())
    result["description"] = re.sub(r"\n{3,}", "\n\n", description).strip()

    email = result.get("contact_email")
    if email:
        email = email.lower().removeprefix("mailto:")
        if EMAIL.match(email):
            result["contact_email"] = email
        else:
            warnings.append(f"invalid contact email: {email}")
            result["contact_email"] = None

    raw_date = result.get("published_at")
    try:
        result["published_at"] = iso_date(raw_date)
    except (ValueError, TypeError, OverflowError):
        warnings.append(f"unrecognized publication date: {raw_date}")
        result["published_at"] = None

    for key in ("salary_min", "salary_max"):
        raw = result.get(key)
        try:
            result[key] = _number(raw)
        except ValueError:
            warnings.append(f"unrecognized {key.replace('_', ' ')}: {raw}")
            result[key] = None
    if result.get("salary_min") is not None and result.get("salary_max") is not None \
            and result["salary_min"] > result["salary_max"]:
        result["salary_min"], result["salary_max"] = result["salary_max"], result["salary_min"]
    if result.get("currency"):
        result["currency"] = result["currency"].upper()

    if result.get("remote") is not None:
        result["remote"] = bool(result["remote"])
    result["employment_types"] = employment_types(result.get("employment_types"),
                                                  f"{result.get('title') or ''}\n{result['description']}")
    result["canonical_url"] = canonical_url(result.get("source_url"))
    result["raw_text"] = result.get("raw_text") or result["description"]
    result["extracted_by"] = dict(result.get("extracted_by") or {})
    result["warnings"] = list(dict.fromkeys(warnings))  # de-duplicate, keep order
    return result
