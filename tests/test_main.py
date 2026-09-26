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
    home = client.get("/opportunities").text
    assert f"/opportunities/{good}" in home and f"/opportunities/{spam}" not in home
    assert f"/opportunities/{spam}" in client.get("/opportunities?rejected=1").text
    assert f"/opportunities/{good}" not in client.get("/opportunities?kind=lead").text
    assert f"/opportunities/{good}" in client.get("/opportunities?kind=job").text


def test_status_buttons(client):
    oid = add(client, "Python dev\nPython")
    client.post(f"/opportunities/{oid}/status", data={"status": "shortlisted"})
    assert db.get_opportunity(oid)["status"] == "shortlisted"
    assert f"/opportunities/{oid}" in client.get("/opportunities?status=shortlisted").text
    assert client.post(f"/opportunities/{oid}/status", data={"status": "deleted"}).status_code == 400


def test_labels_are_saved_and_exported(client):
    oid = add(client, "Python dev\nPython work")
    client.post(f"/opportunities/{oid}/labels", data={"type": "lead", "work_mode": "remote", "relevant": "no"})
    assert db.get_opportunity(oid)["labels"] == {"type": "lead", "work_mode": "remote", "relevant": False}
    assert f"/opportunities/{oid}" in client.get("/opportunities?kind=lead").text  # your label overrides Laya's type
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
                                      "min_salary": "70000", "currency": "usd", "confidence_threshold": "0.7",
                                      "notify_score": "75"},
                    follow_redirects=False)
    assert r.status_code == 303
    after = pipeline.load_profile()
    assert after["name"] == "Kyle" and after["skills"] == ["python", "go"]
    assert after["work_modes"] == ["remote", "hybrid"] and after["min_salary"] == 70000.0
    assert after["currency"] == "USD" and after["confidence_threshold"] == 0.7 and after["notify_score"] == 75
    assert after["questions"] == before["questions"] and after["reject_types"] == before["reject_types"]
    bad = client.post("/profile", data={"confidence_threshold": "5"})
    assert bad.status_code == 422 and pipeline.load_profile() == after
    assert client.post("/profile", data={"notify_score": "150"}).status_code == 422


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
    assert "5 new, 1 duplicate," in page and "1 error" in page


def test_dashboard_summarises_and_guides_setup(client):
    empty = client.get("/").text
    assert "Finish setting up" in empty and "No new opportunities" in empty
    oid = add(client, "Python dev\nPython and SQL. Remote.")
    client.post(f"/opportunities/{oid}/status", data={"status": "shortlisted"})
    page = client.get("/").text
    assert 'href="/opportunities?status=shortlisted"' in page and "No new opportunities" in page
    db.set_status(oid, "new")
    assert f"/opportunities/{oid}" in client.get("/").text  # shown under top new matches


def test_quick_status_returns_to_the_list_but_never_off_site(client):
    oid = add(client, "Python dev\nPython")
    r = client.post(f"/opportunities/{oid}/status", data={"status": "ignored", "next": "/opportunities?kind=job"},
                    follow_redirects=False)
    assert r.headers["location"] == "/opportunities?kind=job" and db.get_opportunity(oid)["status"] == "ignored"
    for bad in ("//evil.example", "/\evil.example", "https://evil.example"):
        r = client.post(f"/opportunities/{oid}/status", data={"status": "new", "next": bad}, follow_redirects=False)
        assert r.headers["location"] == f"/opportunities/{oid}"


def test_list_page_offers_its_jobs_and_adds_the_picked_ones(client, web):
    from tests.test_extract import list_page
    routes, _ = web
    routes["https://b.example/jobs"] = httpx.Response(
        200, text=list_page("python-dev-111111", "sql-dev-222222", "go-dev-333333"), headers={"content-type": "text/html"})
    for slug in ("python-dev-111111", "sql-dev-222222"):
        routes[f"https://b.example/board/job/{slug}"] = httpx.Response(
            200, text=f"<html><title>{slug}</title><body><main><p>Python work</p></main></body></html>",
            headers={"content-type": "text/html"})
    page = client.post("/add", data={"url": "https://b.example/jobs"})
    assert "This page lists 3 jobs" in page.text and "Python dev" in page.text
    assert db.list_opportunities() == []  # the list page itself isn't stored
    r = client.post("/add/many", data={"urls": ["https://b.example/board/job/python-dev-111111",
                                               "https://b.example/board/job/sql-dev-222222"]}, follow_redirects=False)
    assert r.status_code == 303 and "added=2" in r.headers["location"]
    assert "Added 2 jobs" in client.get(r.headers["location"]).text
    page = client.post("/add", data={"url": "https://b.example/jobs"}).text
    assert page.count("already added") == 2
    bad = client.post("/add/many", data={"urls": ["https://b.example/board/job/go-dev-333333"]})
    assert bad.status_code == 422 and "HTTP 404" in bad.text


def test_list_hides_spam_and_ignored_unless_asked(client):
    job = add(client, "Python dev\nPython and SQL. Remote.")
    junk = add(client, "Job board home\nThousands of jobs")
    client.post(f"/opportunities/{junk}/labels", data={"type": "spam"})
    ignored = add(client, "Other dev\nPython")
    client.post(f"/opportunities/{ignored}/status", data={"status": "ignored"})
    page = client.get("/opportunities").text
    assert f"/opportunities/{job}" in page and f"/opportunities/{junk}\"" not in page and f"/opportunities/{ignored}\"" not in page
    assert f"/opportunities/{junk}\"" in client.get("/opportunities?rejected=1").text
    assert f"/opportunities/{ignored}\"" in client.get("/opportunities?status=ignored").text


def test_delete_removes_opportunity_and_its_drafts(client):
    oid = add(client, "Python dev\nPython")
    client.post(f"/opportunities/{oid}/draft/job_application", data={"content": "Hi"})
    r = client.post(f"/opportunities/{oid}/delete", follow_redirects=False)
    assert r.status_code == 303 and db.get_opportunity(oid) is None and db.list_drafts(oid) == []
    assert "Opportunity deleted" in client.get(r.headers["location"]).text


def test_sources_page_shows_health_and_pauses_and_dashboard_warns(client):
    from app import crawl
    url = "https://jobs.example/feed.xml"
    crawl.SOURCES_PATH.write_text(json.dumps([{"kind": "rss", "url": url, "name": "Example feed"}]), encoding="utf-8")
    assert "Not run yet" in client.get("/sources").text
    state = db.get_source_state(f"rss:{url}")
    state.update(error_count=2, last_error="https://jobs.example/feed.xml returned HTTP 404")
    db.put_source_state(state)
    assert "Failing 2 runs" in client.get("/sources").text
    assert "has failed 2 crawls in a row" in client.get("/").text
    client.post("/sources/0/pause")
    assert json.loads(crawl.SOURCES_PATH.read_text(encoding="utf-8"))[0]["paused"] is True
    page = client.get("/sources").text
    assert "Paused" in page and "Resume" in page
    client.post("/sources/0/pause")
    assert "paused" not in json.loads(crawl.SOURCES_PATH.read_text(encoding="utf-8"))[0]


def test_new_source_kinds_can_be_added_from_the_form(client):
    from app import crawl
    client.post("/sources", data={"kind": "remotive", "url": "https://remotive.com/api/remote-jobs?search=python",
                                  "max_runs_per_day": "6"})
    client.post("/sources", data={"kind": "hn", "thread": "seeking_freelancer", "max_runs_per_day": "1"})
    client.post("/sources", data={"kind": "imap", "label": "job-alerts"})
    saved = json.loads(crawl.SOURCES_PATH.read_text(encoding="utf-8"))
    assert saved[0]["max_runs_per_day"] == 4 and saved[1]["thread"] == "seeking_freelancer"
    assert [s["kind"] for s in crawl.load_sources()] == ["remotive", "hn", "imap"]
    assert "Pick a Hacker News thread" in client.post("/sources", data={"kind": "hn", "thread": "x"}).text


def test_keyword_suggestions_become_sources_with_one_click(client, ollama):
    from app import crawl
    crawl.SOURCES_PATH.write_text(json.dumps([{"kind": "page", "url":
        "https://www.onlinejobs.ph/jobseekers/jobsearch?jobkeyword=python"}]), encoding="utf-8")
    assert "Shortlist a few jobs first" in client.post("/sources/suggest-keywords").text
    oid = add(client, "Web scraping developer\nBuild crawlers with Python and FastAPI.")
    client.post(f"/opportunities/{oid}/status", data={"status": "shortlisted"})
    ollama.reply = {"message": {"content": '{"keywords": ["web scraping", "python", "fastapi"]}'}, "done": True}
    page = client.post("/sources/suggest-keywords").text
    assert "4 new suggestions from 1 shortlisted job" in page  # "python" is already searched: 2 terms x 2 boards
    [first] = [x for x in db.list_suggestions() if x["url"].endswith("jobkeyword=web+scraping")]
    client.post(f"/sources/suggestions/{first['id']}/add")
    assert crawl.load_sources()[-1]["url"].endswith("jobkeyword=web+scraping")
    [other] = [x for x in db.list_suggestions() if x["kind"] == "remotive" and x["url"].endswith("fastapi")]
    client.post(f"/sources/suggestions/{other['id']}/dismiss")
    client.post("/sources/suggest-keywords")  # the same terms again
    urls = [x["url"] for x in db.list_suggestions()]
    assert other["url"] not in urls and first["url"] not in urls  # dismissed and added ones don't come back
