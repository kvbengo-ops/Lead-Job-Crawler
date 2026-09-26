"""Clean and validate an extracted opportunity. Problems become warnings; nothing is silently dropped."""
from __future__ import annotations

import re
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

TRACKING_PARAMS = {"fbclid", "gclid", "msclkid", "mc_cid", "mc_eid", "ref", "ref_src", "_hsenc", "_hsmi", "igshid"}
EMAIL = re.compile(r"^[\w.+-]+@[\w-]+(\.[\w-]+)+$")


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
    result["canonical_url"] = canonical_url(result.get("source_url"))
    result["raw_text"] = result.get("raw_text") or result["description"]
    result["extracted_by"] = dict(result.get("extracted_by") or {})
    result["warnings"] = list(dict.fromkeys(warnings))  # de-duplicate, keep order
    return result
