"""Turn pasted text, HTML pages, and URLs into opportunity records (see PLAN.md, "Extractor").

Order: schema.org JobPosting JSON-LD first, then the page's own title/meta/main text. Missing fields stay
None with a warning. Fetching checks robots.txt and identifies itself honestly.
"""
from __future__ import annotations

import json
import re
from html import unescape
from html.parser import HTMLParser
from urllib.parse import urlsplit
from urllib.robotparser import RobotFileParser

import httpx

from .normalize import normalize

USER_AGENT = "LocalJobCrawler/0.1 (personal job search; runs on one PC)"
ROBOTS_AGENT = "LocalJobCrawler"
MAX_BYTES = 5 * 1024 * 1024
TIMEOUT = 10
EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)*\.[A-Za-z]{2,}")
# Yearly equivalents, so an hourly rate is never compared with a yearly minimum.
PER_YEAR = {"HOUR": 2080, "DAY": 260, "WEEK": 52, "MONTH": 12, "YEAR": 1}
KEY_FIELDS = ("title", "company", "location", "published_at")


class FetchError(Exception):
    """A URL could not be fetched or is not allowed to be fetched."""


def make_client() -> httpx.Client:
    # Tests replace this to inject a mock transport.
    return httpx.Client(timeout=TIMEOUT, follow_redirects=True, headers={"User-Agent": USER_AGENT})


_robots: dict[str, RobotFileParser | None] = {}  # per scheme://host for this process; None = allow all


def robots_allowed(url: str, client: httpx.Client) -> bool:
    parts = urlsplit(url)
    base = f"{parts.scheme}://{parts.netloc}"
    if base not in _robots:
        try:
            r = client.get(base + "/robots.txt")
        except httpx.HTTPError:
            return False  # can't read the rules: don't crawl (RFC 9309 treats unreachable as disallow)
        if r.status_code >= 500:
            return False
        if r.status_code >= 400:
            _robots[base] = None  # no robots.txt: everything allowed
        else:
            parser = RobotFileParser()
            parser.parse(r.text.splitlines())
            _robots[base] = parser
    parser = _robots[base]
    return parser is None or parser.can_fetch(ROBOTS_AGENT, url)


def fetch(url: str, *, check_robots: bool = True, headers: dict | None = None,
          client: httpx.Client | None = None, max_bytes: int | None = None) -> httpx.Response:
    """GET with the crawler's User-Agent, a size cap, and (for pages) a robots.txt check.
    Returns 304 responses as-is; raises FetchError for anything else that isn't a 2xx."""
    if urlsplit(url).scheme not in ("http", "https"):
        raise FetchError(f"not an http(s) URL: {url}")
    own = client is None
    client = client or make_client()
    try:
        if check_robots and not robots_allowed(url, client):
            raise FetchError(f"robots.txt does not allow fetching {url}")
        try:
            r = client.get(url, headers=headers or {})
        except httpx.HTTPError as e:
            raise FetchError(f"could not fetch {url}: {e}") from e
        if r.status_code == 304:
            return r
        if r.status_code >= 400:
            raise FetchError(f"{url} returned HTTP {r.status_code}")
        limit = max_bytes or MAX_BYTES
        if len(r.content) > limit:
            raise FetchError(f"{url} is larger than {limit // 1024 // 1024} MB")
        return r
    finally:
        if own:
            client.close()


# --- HTML -----------------------------------------------------------------------

SKIP = {"script", "style", "noscript", "template", "svg", "iframe"}  # never text
CHROME = {"nav", "header", "footer", "form", "aside"}  # page chrome, skipped only outside <main>/<article>
BLOCK = {"p", "div", "br", "li", "ul", "ol", "h1", "h2", "h3", "h4", "h5", "h6", "tr", "section", "article",
         "main", "table", "blockquote", "pre", "dt", "dd", "header", "footer"}
VOID = {"br", "meta", "img", "input", "hr", "link", "source", "wbr", "area", "base", "col", "embed", "track"}


class _Page(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.title, self.meta, self.jsonld = "", {}, []
        self.body, self.main = [], []
        self._skipping: list[str] = []  # open tags whose text is being skipped
        self._main = 0                  # depth inside <main>/<article>
        self._in_title = False
        self._ld = None                 # buffer while inside a JSON-LD script

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "script" and (a.get("type") or "").lower() == "application/ld+json":
            self._ld = []
        if tag == "title" and not self._skipping:
            self._in_title = True
        if tag == "meta":
            key = (a.get("property") or a.get("name") or "").lower()
            if key and a.get("content"):
                self.meta.setdefault(key, a["content"])
        if tag in VOID:
            if tag == "br":
                self._text("\n")
            return
        if tag in SKIP or (tag in CHROME and not self._main):
            self._skipping.append(tag)
        if tag in ("main", "article"):
            self._main += 1
        if tag in BLOCK:
            self._text("\n")

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in VOID:
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        if tag == "script" and self._ld is not None:
            self.jsonld.append("".join(self._ld))
            self._ld = None
        if tag == "title":
            self._in_title = False
        if tag in self._skipping:
            # Close up to the matching tag, so a missing end tag inside it can't leave text skipped forever.
            while self._skipping and self._skipping.pop() != tag:
                pass
        if tag in ("main", "article") and self._main:
            self._main -= 1
        if tag in BLOCK and tag != "li":  # the next <li> starts its own line; no blank line between items
            self._text("\n")

    def handle_data(self, data):
        if self._ld is not None:
            self._ld.append(data)
        elif self._in_title:
            self.title += data
        elif not self._skipping:
            self._text(data)

    def _text(self, s):
        self.body.append(s)
        if self._main:
            self.main.append(s)


def _join(parts: list[str]) -> str:
    text = "".join(parts)
    lines = [re.sub(r"\s+", " ", line).strip() for line in text.split("\n")]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def html_to_text(html: str) -> str:
    page = _Page()
    page.feed(html)
    page.close()
    return _join(page.body)


def _looks_like_html(text: str) -> bool:
    return re.search(r"<(html|head|body|div|p|span|script|title|meta|br|article|main)\b", text, re.I) is not None


def _types(node: dict) -> list[str]:
    t = node.get("@type")
    return [str(x) for x in t] if isinstance(t, list) else [str(t)] if t else []


def _find_jobposting(node, depth=0):
    if depth > 5:
        return None
    if isinstance(node, list):
        for x in node:
            found = _find_jobposting(x, depth + 1)
            if found:
                return found
    elif isinstance(node, dict):
        if "JobPosting" in _types(node):
            return node
        if "@graph" in node:
            return _find_jobposting(node["@graph"], depth + 1)
    return None


def _name(value) -> str | None:
    if isinstance(value, dict):
        return value.get("name")
    return value if isinstance(value, str) else None


def _location(job: dict) -> str | None:
    locs = job.get("jobLocation")
    locs = locs if isinstance(locs, list) else [locs] if locs else []
    names = []
    for loc in locs:
        if not isinstance(loc, dict):
            continue
        addr = loc.get("address")
        if isinstance(addr, dict):
            parts = [addr.get("addressLocality"), addr.get("addressRegion"), _name(addr.get("addressCountry"))]
            name = ", ".join(p for p in parts if isinstance(p, str) and p)
        else:
            name = addr if isinstance(addr, str) else loc.get("name")
        if name and name not in names:
            names.append(name)
    return "; ".join(names) or None


def _salary(job: dict, warnings: list) -> tuple:
    base = job.get("baseSalary")
    if not isinstance(base, dict):
        return None, None, None
    value = base.get("value")
    unit = (value.get("unitText") if isinstance(value, dict) else None) or base.get("unitText") or "YEAR"
    if isinstance(value, dict):
        lo, hi = value.get("minValue", value.get("value")), value.get("maxValue", value.get("value"))
    else:
        lo = hi = value
    factor = PER_YEAR.get(str(unit).upper())
    try:
        lo = float(lo) if lo not in (None, "") else None
        hi = float(hi) if hi not in (None, "") else None
    except (TypeError, ValueError):
        warnings.append(f"unrecognized salary: {value}")
        return None, None, None
    if factor is None:
        warnings.append(f"salary unit {unit} not understood; salary ignored")
        return None, None, None
    if factor != 1:
        warnings.append(f"salary given per {str(unit).lower()}; converted to a yearly amount")
        lo, hi = (lo * factor if lo is not None else None), (hi * factor if hi is not None else None)
    return lo, hi, base.get("currency")


def guess_remote(text: str) -> bool | None:
    if re.search(r"\b(?:not|no|non|isn't)[- ](?:an? |fully )?remote\b", text, re.I):
        return False
    return True if re.search(r"\bremote\b", text, re.I) else None


def _first_email(text: str) -> str | None:
    m = EMAIL.search(text or "")
    return m.group(0) if m else None


def finish(op: dict) -> dict:
    for field in KEY_FIELDS:
        if not op.get(field):
            op["warnings"].append(f"{field.replace('_', ' ')} not found")
    if op.get("salary_min") is None and op.get("salary_max") is None:
        op["warnings"].append("salary not found")
    return normalize(op)


def from_html(html: str, source_url: str | None = None) -> dict:
    page = _Page()
    page.feed(html)
    page.close()
    text = _join(page.main) or _join(page.body)
    warnings: list[str] = []

    for block in page.jsonld:
        try:
            job = _find_jobposting(json.loads(block.strip()))
        except ValueError:
            warnings.append("invalid JSON-LD block ignored")
            continue
        if not job:
            continue
        description = html_to_text(unescape(str(job.get("description") or ""))) or text
        lo, hi, currency = _salary(job, warnings)
        location = _location(job)
        telecommute = "TELECOMMUTE" in str(job.get("jobLocationType", "")).upper()
        op = {
            "source_url": source_url, "title": job.get("title") or job.get("name"),
            "company": _name(job.get("hiringOrganization")), "description": description,
            "location": location or ("Remote" if telecommute else None),
            "remote": True if telecommute else guess_remote(description),
            "salary_min": lo, "salary_max": hi, "currency": currency,
            "contact_email": _first_email(description), "published_at": job.get("datePosted"),
            "raw_text": text, "warnings": warnings,
        }
        op["extracted_by"] = {k: "jsonld" for k in ("title", "company", "description", "location", "salary_min",
                                                    "salary_max", "currency", "published_at") if op.get(k)}
        return finish(op)

    title = (page.meta.get("og:title") or page.title or "").strip() or None
    description = text or page.meta.get("og:description") or page.meta.get("description") or ""
    op = {
        "source_url": source_url, "title": title, "company": page.meta.get("og:site_name"),
        "description": description, "location": None, "remote": guess_remote(description),
        "salary_min": None, "salary_max": None, "currency": None,
        "contact_email": _first_email(description), "published_at": page.meta.get("article:published_time"),
        "raw_text": text, "warnings": warnings,
    }
    op["extracted_by"] = {k: "html" for k in ("title", "company", "description", "contact_email", "published_at")
                          if op.get(k)}
    return finish(op)


def from_text(text: str, source_url: str | None = None) -> dict:
    """Pasted text (or HTML). For plain text, a short first line becomes the title."""
    text = (text or "").strip()
    if _looks_like_html(text):
        return from_html(text, source_url)
    lines = text.splitlines()
    first = lines[0].strip() if lines else ""
    rest = "\n".join(lines[1:]).strip()
    title = first if first and rest and len(first) <= 150 else None
    description = rest if title else text
    op = {
        "source_url": source_url, "title": title, "company": None, "description": description,
        "location": None, "remote": guess_remote(text), "salary_min": None, "salary_max": None, "currency": None,
        "contact_email": _first_email(text), "published_at": None, "raw_text": text, "warnings": [],
    }
    op["extracted_by"] = {k: "user" for k in ("title", "description", "contact_email") if op.get(k)}
    return finish(op)


def from_url(url: str) -> dict:
    """Fetch one page (robots.txt permitting) and extract it. Raises FetchError on failure."""
    r = fetch(url)
    ctype = r.headers.get("content-type", "").lower()
    if ctype and not any(t in ctype for t in ("html", "xml", "text/plain")):
        raise FetchError(f"{url} is {ctype.split(';')[0]}, not a web page")
    final = str(r.url)
    return from_html(r.text, final) if "html" in ctype or _looks_like_html(r.text) else from_text(r.text, final)
