import json

import httpx
import pytest
from fastapi.testclient import TestClient

from app import db, pipeline
from app.main import app


@pytest.fixture
def client(laya):
    return TestClient(app)


def add(client, text):
    r = client.post("/add", data={"text": text}, follow_redirects=False)
    assert r.status_code == 303, r.text
    return int(r.headers["location"].rsplit("/", 1)[1].split("?")[0])


def test_scraped_html_is_escaped(client):
    oid = add(client, "<script>alert(1)</script> Job\nPython <img src=x onerror=alert(2)> work")
    page = client.get(f"/opportunities/{oid}").text
    assert "<script>alert(1)" not in page and "<img src=x" not in page
    assert "&lt;img src=x onerror=alert(2)&gt;" in page or "onerror" not in page
    assert "<script>alert(1)" not in client.get("/").text


def test_javascript_source_url_is_not_linked(client):
    oid, _ = pipeline.process(op={"source_url": "javascript:alert(1)", "title": "Job", "description": "Python"})
    assert 'href="javascript:' not in client.get(f"/opportunities/{oid}").text


def test_list_filters(client):
    good = add(client, "Python dev\nPython and SQL. Remote.")
    spam = add(client, "Spam\nThis is spam")
    home = client.get("/").text
    assert f"/opportunities/{good}" in home and f"/opportunities/{spam}" not in home
    assert f"/opportunities/{spam}" in client.get("/?rejected=1").text
    assert f"/opportunities/{good}" not in client.get("/?kind=lead").text
    assert f"/opportunities/{good}" in client.get("/?kind=job").text


def test_status_buttons(client):
    oid = add(client, "Python dev\nPython")
    client.post(f"/opportunities/{oid}/status", data={"status": "shortlisted"})
    assert db.get_opportunity(oid)["status"] == "shortlisted"
    assert f"/opportunities/{oid}" in client.get("/?status=shortlisted").text
    assert client.post(f"/opportunities/{oid}/status", data={"status": "deleted"}).status_code == 400


def test_labels_are_saved_and_exported(client):
    oid = add(client, "Python dev\nPython work")
    client.post(f"/opportunities/{oid}/labels", data={"type": "lead", "work_mode": "remote", "relevant": "no"})
    assert db.get_opportunity(oid)["labels"] == {"type": "lead", "work_mode": "remote", "relevant": False}
    assert f"/opportunities/{oid}" in client.get("/?kind=lead").text  # your label overrides Laya's type
    lines = client.get("/labels.jsonl").text.splitlines()
    assert [json.loads(line) for line in lines] == [
        {"title": "Python dev", "text": "Python work", "type": "lead", "work_mode": "remote", "relevant": False}]
    assert client.post(f"/opportunities/{oid}/labels", data={"type": "made-up"}).status_code == 400


def test_add_errors_are_shown(client, web):
    r = client.post("/add", data={"text": "  "})
    assert r.status_code == 422 and "Nothing to add" in r.text
    r = client.post("/add", data={"url": "https://nowhere.example/job"})
    assert r.status_code == 422 and "HTTP 404" in r.text


def test_duplicate_url_redirects_to_original(client, web):
    routes, _ = web
    routes["https://site.example/job"] = httpx.Response(200, text="<title>Job</title><p>Python</p>",
                                                        headers={"content-type": "text/html"})
    first = client.post("/add", data={"url": "https://site.example/job"}, follow_redirects=False)
    again = client.post("/add", data={"url": "https://site.example/job/?utm_source=x"}, follow_redirects=False)
    assert again.headers["location"] == first.headers["location"] + "?notice=duplicate"


def test_reevaluate_reports_laya_down(client, laya):
    oid = add(client, "Python dev\nPython")
    laya.down = True
    r = client.post(f"/opportunities/{oid}/evaluate", follow_redirects=False)
    assert r.headers["location"].endswith("notice=laya_down")


def test_drafts_create_edit_download(client):
    oid = add(client, "Python dev\nPython work")
    page = client.get(f"/opportunities/{oid}/draft/job_application").text
    assert "Application for Python dev" in page and "Test User" in page
    r = client.post(f"/opportunities/{oid}/draft/job_application", data={"content": "Hello <b>there</b>"},
                    follow_redirects=False)
    did = int(r.headers["location"].split("/")[2].split("?")[0])
    assert "Hello &lt;b&gt;there&lt;/b&gt;" in client.get(f"/drafts/{did}").text
    client.post(f"/drafts/{did}", data={"content": "Edited"})
    md = client.get(f"/drafts/{did}.md")
    assert md.text == "Edited" and "attachment" in md.headers["content-disposition"]
    assert client.get(f"/opportunities/{oid}/draft/sonnet").status_code == 404


def test_profile_page_keeps_questions_and_weights(client):
    before = pipeline.load_profile()
    r = client.post("/profile", data={"name": "Kyle", "skills": "python, go", "work_modes": ["remote", "hybrid"],
                                      "min_salary": "70000", "currency": "usd", "confidence_threshold": "0.7"},
                    follow_redirects=False)
    assert r.status_code == 303
    after = pipeline.load_profile()
    assert after["name"] == "Kyle" and after["skills"] == ["python", "go"]
    assert after["work_modes"] == ["remote", "hybrid"] and after["min_salary"] == 70000.0
    assert after["currency"] == "USD" and after["confidence_threshold"] == 0.7
    assert after["questions"] == before["questions"] and after["reject_types"] == before["reject_types"]
    bad = client.post("/profile", data={"confidence_threshold": "5"})
    assert bad.status_code == 422 and pipeline.load_profile() == after


def test_cross_site_posts_are_refused(client):
    r = client.post("/add", data={"text": "x"}, headers={"origin": "https://evil.example"})
    assert r.status_code == 403
    r = client.post("/add", data={"text": "Job\nPython"}, headers={"origin": "http://testserver"},
                    follow_redirects=False)
    assert r.status_code == 303


def test_last_run_and_new_count_are_shown(client):
    client.get("/")  # first visit
    add(client, "Python dev\nPython")
    db.record_run({"started_at": "2026-09-26T08:00:00+00:00", "ended_at": "2026-09-26T08:01:00+00:00",
                   "sources_fetched": 2, "new_items": 5, "duplicates": 1, "pending": 0, "errors": ["x: HTTP 500"]})
    page = client.get("/").text
    assert "5 new, 1 duplicates" in page and "1 error" in page
