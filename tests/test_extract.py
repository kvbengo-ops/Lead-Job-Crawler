from pathlib import Path

import httpx
import pytest

from app import extract
from app.extract import FetchError, from_html, from_text, from_url
from app.normalize import canonical_url, normalize

FIXTURES = Path(__file__).parent / "fixtures"


def fixture(name):
    return (FIXTURES / name).read_text(encoding="utf-8")


# --- the three saved pages -------------------------------------------------------

def test_jsonld_page():
    op = from_html(fixture("job_jsonld.html"), "https://acme.example/jobs/1")
    assert op["title"] == "Senior Python Engineer"
    assert op["company"] == "Acme"
    assert op["remote"] is True and op["location"] == "Remote"
    assert op["description"].startswith("Build FastAPI services and SQL pipelines.")
    assert "<" not in op["description"]
    assert op["contact_email"] == "jobs@acme.example"
    assert op["published_at"] == "2026-09-01"
    assert (op["salary_min"], op["salary_max"], op["currency"]) == (60 * 2080, 80 * 2080, "USD")
    assert "salary given per hour; converted to a yearly amount" in op["warnings"]
    assert op["extracted_by"]["title"] == "jsonld"
    assert "Home | Jobs" not in op["raw_text"] and "Copyright" not in op["raw_text"]


def test_page_without_jsonld_uses_main_content():
    op = from_html(fixture("job_plain.html"), "https://globex.example/careers/data")
    assert op["title"] == "Data Engineer | Globex"
    assert op["company"] == "Globex"
    text = op["description"]
    assert "Data Engineer" in text and "Berlin, Germany" in text  # the article's own <header> is kept
    assert "Design ETL pipelines in Python & SQL.\nMaintain our web scrapers." in text
    assert "3+ years of Python" in text
    assert "Products Careers" not in text and "Imprint" not in text and "tracking" not in text
    assert op["remote"] is False  # "not a remote position"
    assert op["extracted_by"]["title"] == "html"
    assert "location not found" in op["warnings"]


def test_non_job_page():
    op = from_html(fixture("not_job.html"), "https://blog.example/sourdough")
    assert op["title"] == "10 Tips for Better Sourdough"
    assert "Feed your starter twice a day." in op["description"]
    assert "invalid JSON-LD block ignored" in op["warnings"]
    assert op["remote"] is None


# --- pasted text -------------------------------------------------------------------

def test_pasted_text_first_line_is_title():
    op = from_text("Backend Developer\n\nWe need Python and SQL.\nFully remote.")
    assert op["title"] == "Backend Developer"
    assert op["description"] == "We need Python and SQL.\nFully remote."
    assert op["remote"] is True
    assert op["extracted_by"]["title"] == "user"


@pytest.mark.parametrize("text", [
    "Just one line about a job",
    "x" * 200 + "\nsecond line",  # a first line this long is a paragraph, not a title
])
def test_pasted_text_without_title(text):
    op = from_text(text)
    assert op["title"] is None
    assert op["description"] == text
    assert "title not found" in op["warnings"]


def test_pasted_html_is_parsed_as_html():
    assert from_text("<html><title>Hi</title><body><p>Job text</p></body></html>")["title"] == "Hi"


# --- normalization -------------------------------------------------------------------

@pytest.mark.parametrize("url, expected", [
    ("HTTPS://Example.COM/jobs/1/?utm_source=x&ref=abc&x=1#top", "https://example.com/jobs/1?x=1"),
    ("https://example.com/jobs/1/", "https://example.com/jobs/1"),
    ("https://example.com/", "https://example.com/"),
    ("https://example.com", "https://example.com/"),
    ("https://example.com:443/a?gclid=1&fbclid=2", "https://example.com/a"),
    ("http://example.com:80/a#frag", "http://example.com/a"),
    ("https://example.com/a?b=2&a=1", "https://example.com/a?b=2&a=1"),
    (None, None),
])
def test_canonical_url(url, expected):
    assert canonical_url(url) == expected


@pytest.mark.parametrize("raw, expected", [
    ("2026-09-01", "2026-09-01"),
    ("2026-09-01T10:00:00Z", "2026-09-01T10:00:00+00:00"),
    ("Tue, 01 Sep 2026 10:00:00 +0200", "2026-09-01T08:00:00+00:00"),
    (1788220800000, "2026-09-01T00:00:00+00:00"),
])
def test_dates_become_iso(raw, expected):
    assert normalize({"published_at": raw})["published_at"] == expected


def test_bad_values_become_warnings_not_errors():
    op = normalize({"published_at": "next Tuesday", "contact_email": "not-an-email", "salary_min": "lots"})
    assert op["published_at"] is None and op["contact_email"] is None and op["salary_min"] is None
    assert op["warnings"] == ["invalid contact email: not-an-email", "unrecognized publication date: next Tuesday",
                              "unrecognized salary min: lots"]


def test_salary_order_and_currency_are_fixed():
    op = normalize({"salary_min": "90,000", "salary_max": 70000, "currency": "eur"})
    assert (op["salary_min"], op["salary_max"], op["currency"]) == (70000.0, 90000.0, "EUR")


# --- fetching ------------------------------------------------------------------------

def test_from_url_respects_robots(web):
    routes, requests = web
    routes["https://site.example/robots.txt"] = httpx.Response(200, text="User-agent: *\nDisallow: /private/")
    routes["https://site.example/jobs/1"] = httpx.Response(200, text=fixture("job_plain.html"),
                                                           headers={"content-type": "text/html"})
    assert from_url("https://site.example/jobs/1")["title"] == "Data Engineer | Globex"
    with pytest.raises(FetchError, match="robots.txt"):
        from_url("https://site.example/private/2")
    assert all(r.headers["user-agent"].startswith("LocalJobCrawler") for r in requests)


def test_missing_robots_allows_and_server_error_blocks(web):
    routes, _ = web
    routes["https://open.example/job"] = httpx.Response(200, text="<p>Job</p>", headers={"content-type": "text/html"})
    assert from_url("https://open.example/job")["description"] == "Job"  # robots.txt 404: allowed
    routes["https://down.example/robots.txt"] = httpx.Response(503)
    with pytest.raises(FetchError, match="robots.txt"):
        from_url("https://down.example/job")


def test_fetch_errors(web):
    routes, _ = web
    routes["https://site.example/gone"] = httpx.Response(410)
    routes["https://site.example/file.pdf"] = httpx.Response(200, content=b"%PDF", headers={"content-type": "application/pdf"})
    with pytest.raises(FetchError, match="HTTP 410"):
        from_url("https://site.example/gone")
    with pytest.raises(FetchError, match="application/pdf"):
        from_url("https://site.example/file.pdf")
    with pytest.raises(FetchError, match="not an http"):
        from_url("file:///etc/passwd")


def test_oversized_page_is_refused(web, monkeypatch):
    routes, _ = web
    monkeypatch.setattr(extract, "MAX_BYTES", 10)
    routes["https://site.example/big"] = httpx.Response(200, text="<p>" + "x" * 100 + "</p>")
    with pytest.raises(FetchError, match="larger than"):
        from_url("https://site.example/big")


# --- job board list pages ------------------------------------------------------------

LIST_PAGE = """<html><title>Jobs</title><body><nav><a href="/jobs/search/c/python">Python</a></nav>
<main>{}</main></body></html>"""


def list_page(*slugs):
    cards = "".join(f'<div><a href="/board/job/{s}">See More</a> <a href="/board/job/{s}#apply">Apply</a></div>'
                    for s in slugs)
    return LIST_PAGE.format(cards)


def test_list_page_is_recognised_and_named_from_urls():
    with pytest.raises(extract.ListingPage) as caught:
        from_html(list_page("python-dev-123456", "data-engineer-234567", "qa-tester-345678"), "https://b.example/jobs")
    assert caught.value.links == [("https://b.example/board/job/python-dev-123456", "Python dev"),
                                  ("https://b.example/board/job/data-engineer-234567", "Data engineer"),
                                  ("https://b.example/board/job/qa-tester-345678", "Qa tester")]


def test_job_page_with_related_jobs_is_still_one_job():
    html = list_page("a-111111", "b-222222", "c-333333").replace("<main>", "<main><p>Python developer wanted</p>")
    assert "Python developer wanted" in from_html(html, "https://b.example/board/job/python-dev-999999")["description"]
    assert from_html(list_page("a-111111", "b-222222"), "https://b.example/jobs")["title"] == "Jobs"  # 2 links: a page
