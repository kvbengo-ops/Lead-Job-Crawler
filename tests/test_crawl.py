import json
import os
import time

import httpx
import pytest

from app import crawl, db, pipeline

GREENHOUSE_URL = "https://boards-api.greenhouse.io/v1/boards/acme/jobs?content=true"
LEVER_URL = "https://api.lever.co/v0/postings/globex?mode=json"
FEED_URL = "https://jobs.example/feed.xml"

GREENHOUSE = {"jobs": [
    {"id": 101, "title": "Python Engineer", "absolute_url": "https://boards.greenhouse.io/acme/jobs/101",
     "location": {"name": "Remote - US"}, "company_name": "Acme", "first_published": "2026-09-20T12:00:00-04:00",
     "content": "&lt;p&gt;Build Python and SQL services.&lt;/p&gt;"},
    {"id": 102, "title": "Office Manager", "absolute_url": "https://boards.greenhouse.io/acme/jobs/102",
     "location": {"name": "Austin, TX"}, "content": "&lt;p&gt;Run the office.&lt;/p&gt;"},
]}
LEVER = [{"id": "abc-1", "text": "Data Engineer", "hostedUrl": "https://jobs.lever.co/globex/abc-1",
          "categories": {"location": "Berlin"}, "workplaceType": "onsite", "descriptionPlain": "Python pipelines.",
          "lists": [{"text": "Requirements", "content": "<li>SQL</li><li>Airflow</li>"}], "additionalPlain": "",
          "createdAt": 1788220800000, "salaryRange": {"min": 40, "max": 50, "currency": "EUR", "interval": "per-hour-wage"}}]
ATOM = """<?xml version="1.0" encoding="utf-8"?>
<feed xmlns="http://www.w3.org/2005/Atom"><title>Jobs</title>
  <entry><id>urn:job:1</id><title>Remote Python Contract</title><link href="https://jobs.example/1"/>
    <updated>2026-09-21T09:00:00Z</updated><summary type="html">&lt;p&gt;Automation work in Python.&lt;/p&gt;</summary></entry>
</feed>"""


def write_sources(sources):
    crawl.SOURCES_PATH.write_text(json.dumps(sources), encoding="utf-8")


@pytest.fixture
def feeds(web):
    routes, requests = web

    def conditional(body, etag, content_type):
        def respond(request):
            if request.headers.get("if-none-match") == etag:
                return httpx.Response(304)
            return httpx.Response(200, content=body, headers={"etag": etag, "content-type": content_type})
        return respond

    routes[GREENHOUSE_URL] = conditional(json.dumps(GREENHOUSE).encode(), '"gh1"', "application/json")
    routes[LEVER_URL] = conditional(json.dumps(LEVER).encode(), '"lv1"', "application/json")
    routes[FEED_URL] = conditional(ATOM.encode(), '"f1"', "application/atom+xml")
    write_sources([{"kind": "greenhouse", "board": "acme"}, {"kind": "lever", "board": "globex", "name": "Globex"},
                   {"kind": "rss", "url": FEED_URL}])
    return routes, requests


def titles():
    return sorted(o["title"] for o in db.list_opportunities(show_rejected=True))


def test_first_run_adds_items_second_run_adds_nothing(feeds, laya):
    assert crawl.run() == 0
    assert titles() == ["Data Engineer", "Office Manager", "Python Engineer", "Remote Python Contract"]
    run = db.last_run()
    assert (run["sources_fetched"], run["new_items"], run["duplicates"], run["errors"]) == (3, 4, 0, [])

    calls = laya.calls
    assert crawl.run() == 0
    assert len(titles()) == 4 and laya.calls == calls
    run = db.last_run()
    assert (run["sources_fetched"], run["new_items"]) == (3, 0)
    _, requests = feeds
    assert requests[-1].headers["if-none-match"] == '"f1"'  # conditional request on the repeat run


def test_records_are_mapped_from_each_source(feeds, laya):
    crawl.run()
    by_title = {o["title"]: o for o in db.list_opportunities(show_rejected=True)}
    gh = by_title["Python Engineer"]
    assert (gh["company"], gh["location"], gh["remote"]) == ("Acme", "Remote - US", True)
    assert gh["description"] == "Build Python and SQL services."
    assert gh["published_at"] == "2026-09-20T16:00:00+00:00"
    assert gh["source_url"] == "https://boards.greenhouse.io/acme/jobs/101"
    lv = db.get_opportunity(by_title["Data Engineer"]["id"])
    assert (lv["company"], lv["location"], lv["remote"]) == ("Globex", "Berlin", False)
    assert "Requirements\nSQL\nAirflow" in lv["description"]
    assert (lv["salary_min"], lv["salary_max"], lv["currency"]) == (40 * 2080, 50 * 2080, "EUR")
    assert lv["published_at"] == "2026-09-01T00:00:00+00:00"
    assert lv["extracted_by"]["title"] == "lever"
    rss = by_title["Remote Python Contract"]
    assert (rss["source_url"], rss["remote"], rss["description"]) == ("https://jobs.example/1", True,
                                                                       "Automation work in Python.")


def test_laya_down_leaves_items_pending_and_next_run_evaluates(feeds, laya):
    laya.down = True
    crawl.run()
    assert db.last_run()["pending"] == 4
    assert {o["status"] for o in db.list_opportunities(show_rejected=True)} == {"pending_evaluation"}
    laya.down = False
    crawl.run()
    assert db.last_run()["pending"] == 0
    assert {o["status"] for o in db.list_opportunities(show_rejected=True)} == {"new"}


def test_max_runs_per_day(feeds, laya):
    write_sources([{"kind": "rss", "url": FEED_URL, "max_runs_per_day": 1}])
    crawl.run()
    crawl.run()
    assert db.last_run()["sources_fetched"] == 0


def test_second_run_exits_while_locked(feeds, laya):
    crawl.LOCK_PATH.write_text("123", encoding="utf-8")
    assert crawl.run() == 0
    assert db.last_run() is None and titles() == []
    assert crawl.LOCK_PATH.exists()  # someone else's lock is left alone


def test_stale_lock_is_taken_over(feeds, laya):
    crawl.LOCK_PATH.write_text("123", encoding="utf-8")
    old = time.time() - crawl.STALE_LOCK_SECONDS - 60
    os.utime(crawl.LOCK_PATH, (old, old))
    assert crawl.run() == 0
    assert len(titles()) == 4
    assert not crawl.LOCK_PATH.exists()


def test_failing_source_is_reported_and_others_continue(feeds, laya):
    write_sources([{"kind": "rss", "url": "https://jobs.example/missing.xml"}, {"kind": "rss", "url": FEED_URL}])
    assert crawl.run() == 1
    run = db.last_run()
    assert run["sources_fetched"] == 1 and run["new_items"] == 1
    assert "HTTP 404" in run["errors"][0]


def test_not_a_feed(web, laya):
    routes, _ = web
    routes["https://jobs.example/page.html"] = httpx.Response(200, text="<html><body>hello</body></html>")
    write_sources([{"kind": "rss", "url": "https://jobs.example/page.html"}])
    assert crawl.run() == 1
    assert "not a readable RSS or Atom feed" in db.last_run()["errors"][0]


def test_page_source_is_fetched_once(web, laya):
    routes, _ = web
    routes["https://site.example/job/9"] = httpx.Response(
        200, text="<html><title>Python Dev</title><body><main><p>Python work</p></main></body></html>",
        headers={"content-type": "text/html"})
    write_sources([{"kind": "page", "url": "https://site.example/job/9", "max_runs_per_day": 5}])
    crawl.run()
    crawl.run()
    assert titles() == ["Python Dev"]
    assert db.last_run()["new_items"] == 0


@pytest.mark.parametrize("sources, message", [
    ({"kind": "rss"}, "must be a JSON list"),
    ([{"kind": "ftp", "url": "x"}], "\"kind\" must be one of"),
    ([{"kind": "greenhouse"}], "needs \"board\""),
    ([{"kind": "lever", "board": "a/b"}], "needs \"board\""),
    ([{"kind": "rss", "url": "jobs.example/feed"}], "http(s) \"url\""),
    ([{"kind": "rss", "url": "https://x.example/f", "max_runs_per_day": 0}], "at least 1"),
])
def test_bad_sources_file_is_reported(sources, message, laya):
    write_sources(sources)
    assert crawl.run() == 1
    assert message in db.last_run()["errors"][0]


def test_no_sources_file_is_fine(laya):
    assert crawl.run() == 0
    assert db.last_run()["sources_fetched"] == 0


def test_list_page_source_adds_its_jobs_and_new_ones_later(web, laya):
    from tests.test_extract import list_page
    routes, _ = web
    html = lambda: httpx.Response(200, text=list_page(*slugs), headers={"content-type": "text/html"})
    slugs = ["python-dev-111111", "sql-dev-222222", "go-dev-333333"]
    routes["https://site.example/jobs"] = lambda request: html()
    for n, slug in enumerate(slugs + ["rust-dev-444444"]):
        routes[f"https://site.example/board/job/{slug}"] = httpx.Response(
            200, text=f"<html><title>Job {n}</title><body><main><p>Python work</p></main></body></html>",
            headers={"content-type": "text/html"})
    write_sources([{"kind": "page", "url": "https://site.example/jobs", "max_runs_per_day": 5}])
    crawl.run()
    assert sorted(titles()) == ["Job 0", "Job 1", "Job 2"] and db.last_run()["errors"] == []
    slugs.append("rust-dev-444444")
    crawl.run()
    assert sorted(titles()) == ["Job 0", "Job 1", "Job 2", "Job 3"] and db.last_run()["new_items"] == 1


def test_source_health_counts_failures_in_a_row_and_resets(web, laya):
    routes, _ = web
    write_sources([{"kind": "rss", "url": FEED_URL, "max_runs_per_day": 9}])
    crawl.run()
    crawl.run()  # the feed isn't there yet: 404 twice
    state = db.get_source_state(f"rss:{FEED_URL}")
    assert state["error_count"] == 2 and "HTTP 404" in state["last_error"] and state["last_ok_at"] is None
    routes[FEED_URL] = httpx.Response(200, content=ATOM.encode(), headers={"content-type": "application/atom+xml"})
    crawl.run()
    state = db.get_source_state(f"rss:{FEED_URL}")
    assert state["error_count"] == 0 and state["last_error"] is None and state["last_ok_at"] and state["last_new"] == 1


def test_list_page_that_stops_linking_to_jobs_is_an_error_not_a_job(web, laya):
    from tests.test_extract import list_page
    routes, _ = web
    page = {"html": list_page("python-dev-111111", "sql-dev-222222", "go-dev-333333")}
    routes["https://site.example/jobs"] = lambda request: httpx.Response(
        200, text=page["html"], headers={"content-type": "text/html"})
    for n, slug in enumerate(["python-dev-111111", "sql-dev-222222", "go-dev-333333", "rust-dev-444444"]):
        routes[f"https://site.example/board/job/{slug}"] = httpx.Response(
            200, text=f"<html><title>Job {n}</title><body><main><p>Python work</p></main></body></html>",
            headers={"content-type": "text/html"})
    write_sources([{"kind": "page", "url": "https://site.example/jobs", "max_runs_per_day": 9}])
    crawl.run()
    assert len(titles()) == 3
    page["html"] = "<html><title>Sign in</title><body><main><p>Please sign in to see jobs.</p></main></body></html>"
    assert crawl.run() == 1
    assert "no job links" in db.last_run()["errors"][0] and len(titles()) == 3  # the login page isn't stored
    assert db.get_source_state("page:https://site.example/jobs")["error_count"] == 1
    page["html"] = list_page("rust-dev-444444")  # one job is enough once the page is known to be a list
    assert crawl.run() == 0 and "Job 3" in titles()


def test_paused_source_is_skipped(feeds, laya):
    write_sources([{"kind": "rss", "url": FEED_URL, "paused": True}])
    assert crawl.run() == 0
    assert db.last_run()["sources_fetched"] == 0 and titles() == []


def test_each_opportunity_records_its_source_and_sources_are_counted(feeds, laya):
    crawl.run()
    stats = db.source_stats()
    assert stats[f"greenhouse:{GREENHOUSE_URL}"]["found"] == 2 and stats[f"rss:{FEED_URL}"]["found"] == 1
    assert all(s["avg_score"] is not None for s in stats.values())


REMOTIVE_URL = "https://remotive.com/api/remote-jobs?search=python"
REMOTIVE = {"jobs": [{"id": 7, "url": "https://remotive.com/remote-jobs/software-dev/python-dev-7", "title": "Python Dev",
                      "company_name": "Initech", "candidate_required_location": "Worldwide",
                      "publication_date": "2026-09-20T10:00:00", "description": "<p>Python and FastAPI.</p>"}]}
REMOTEOK = [{"legal": "API Terms of Service: link back to Remote OK."},
            {"id": "99", "url": "https://remoteok.com/remote-jobs/99", "position": "Automation Engineer",
             "company": "Hooli", "location": "Remote", "date": "2026-09-21T08:00:00+00:00",
             "description": "<p>Python automation.</p>"}]


def json_route(data):
    return httpx.Response(200, content=json.dumps(data).encode(), headers={"content-type": "application/json"})


def test_remotive_and_remoteok_sources(web, laya):
    routes, _ = web
    routes[REMOTIVE_URL] = json_route(REMOTIVE)
    routes["https://remoteok.com/api"] = json_route(REMOTEOK)
    write_sources([{"kind": "remotive", "url": REMOTIVE_URL, "max_runs_per_day": 6},
                   {"kind": "remoteok", "url": "https://remoteok.com/api"}])
    assert crawl.run() == 0
    assert titles() == ["Automation Engineer", "Python Dev"]  # Remote OK's legal notice isn't a job
    assert crawl.load_sources()[0]["max_runs_per_day"] == 4  # Remotive allows 4 requests a day


def hn_routes(routes, thread, title, comments):
    params, _, _ = crawl.HN_THREADS[thread]
    search = str(httpx.URL(crawl.HN_SEARCH, params={**params, "hitsPerPage": 20}))
    routes[search] = json_route({"hits": [{"objectID": "500", "title": "Ask HN: Something else"},
                                          {"objectID": "501", "title": title}]})
    routes[crawl.HN_COMMENTS.format("501")] = json_route({"hits": comments})


def test_hn_who_is_hiring_keeps_top_level_comments(web, laya):
    routes, _ = web
    hn_routes(routes, "who_is_hiring", "Ask HN: Who is hiring? (September 2026)", [
        {"objectID": "1", "parent_id": 501, "comment_text": "Acme | Python Engineer | Remote (US)<p>We build APIs.",
         "created_at": "2026-09-01T16:00:00Z"},
        {"objectID": "2", "parent_id": 1, "comment_text": "Is this role open to Europe?"},  # a reply
    ])
    write_sources([{"kind": "hn", "thread": "who_is_hiring"}])
    assert crawl.run() == 0
    [o] = db.list_opportunities(show_rejected=True)
    assert (o["title"], o["company"]) == ("Python Engineer", "Acme")


def test_hn_freelancer_thread_keeps_only_clients(web, laya):
    routes, _ = web
    hn_routes(routes, "seeking_freelancer", "Ask HN: Freelancer? Seeking freelancer? (September 2026)", [
        {"objectID": "3", "parent_id": 501, "comment_text": "SEEKING FREELANCER | Web scraping | Remote<p>Need a crawler."},
        {"objectID": "4", "parent_id": 501, "comment_text": "SEEKING WORK | Python developer | Remote"},
    ])
    write_sources([{"kind": "hn", "thread": "seeking_freelancer"}])
    assert crawl.run() == 0
    assert titles() == ["Web scraping"]


def test_hn_thread_not_found_is_a_source_error(web, laya):
    routes, _ = web
    params, _, _ = crawl.HN_THREADS["who_is_hiring"]
    routes[str(httpx.URL(crawl.HN_SEARCH, params={**params, "hitsPerPage": 20}))] = json_route({"hits": []})
    write_sources([{"kind": "hn", "thread": "who_is_hiring"}])
    assert crawl.run() == 1 and "no Hacker News thread" in db.last_run()["errors"][0]


def test_hn_source_needs_a_known_thread(laya):
    write_sources([{"kind": "hn", "thread": "whatever"}])
    assert crawl.run() == 1 and "hn needs \"thread\"" in db.last_run()["errors"][0]


def test_strong_new_match_gets_an_ai_draft_and_one_notification(web, laya, ollama, monkeypatch, tmp_path):
    calls = []
    script = tmp_path / "notify.ps1"
    script.write_text("# fake", encoding="utf-8")
    monkeypatch.setattr(crawl, "NOTIFY_SCRIPT", script)
    monkeypatch.setattr(crawl.os, "name", "nt")
    monkeypatch.setattr(crawl.subprocess, "run", lambda args, **kw: calls.append(args) or
                        type("Done", (), {"returncode": 0, "stderr": ""})())
    routes, _ = web
    routes[FEED_URL] = httpx.Response(200, content=ATOM.encode(), headers={"content-type": "application/atom+xml"})
    write_sources([{"kind": "rss", "url": FEED_URL}])
    db.save_profile({**pipeline.load_profile(), "notify_score": 70})
    crawl.run()
    [o] = db.list_opportunities()
    assert o["score"] >= 70
    [draft] = db.list_drafts(o["id"])
    assert draft["status"] == "ai_generated" and draft["kind"] == "job_application"
    [args] = calls
    assert args[args.index("-Title") + 1] == "1 new strong match"
    assert "1 AI draft ready" in args[args.index("-Body") + 1]
    assert args[args.index("-Url") + 1].endswith(f"/opportunities/{o['id']}")


def test_no_notification_without_strong_matches_and_ollama_down_is_fine(web, laya, monkeypatch):
    calls = []
    monkeypatch.setattr(crawl, "notify", lambda *a: calls.append(a))
    routes, _ = web
    routes[FEED_URL] = httpx.Response(200, content=ATOM.encode(), headers={"content-type": "application/atom+xml"})
    write_sources([{"kind": "rss", "url": FEED_URL}])
    db.save_profile({**pipeline.load_profile(), "notify_score": 101})  # nothing can reach it
    assert crawl.run() == 0 and calls == []
    db.save_profile({**pipeline.load_profile(), "notify_score": 0})
    another = ATOM.replace("urn:job:1", "urn:job:2").replace("jobs.example/1", "jobs.example/2")
    routes[FEED_URL] = httpx.Response(200, content=another.encode(), headers={"content-type": "application/atom+xml"})
    write_sources([{"kind": "rss", "url": FEED_URL, "max_runs_per_day": 9}])
    assert crawl.run() == 0  # Ollama is down in tests: no draft, but the notification still comes
    [(title, body, url)] = calls
    assert "strong match" in title and "draft" not in body


def test_source_reaching_two_failures_is_notified_once(web, laya, monkeypatch):
    calls = []
    monkeypatch.setattr(crawl, "notify", lambda *a: calls.append(a))
    write_sources([{"kind": "rss", "url": FEED_URL, "max_runs_per_day": 9}])
    for _ in range(3):
        crawl.run()  # 404 each time
    [(title, body, url)] = calls
    assert title == "A crawler source keeps failing" and "HTTP 404" in body and url.endswith("/sources")


class FakeMailbox:
    """Stands in for imaplib.IMAP4_SSL with one alert email in the "job-alerts" label."""
    EMAIL = b"From: jobs-noreply@linkedin.com\r\nSubject: 2 new jobs\r\nMessage-ID: <a1@example>\r\n\r\nNew jobs for you.\r\n"

    def __init__(self, host, timeout=None):
        self.host = host

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def login(self, user, password):
        assert (user, password) == ("me@example.com", "app-password")

    def select(self, label, readonly=False):
        assert readonly  # the crawler never changes the mailbox
        return ("OK", [b"1"]) if label == '"job-alerts"' else ("NO", [b"no such label"])

    def search(self, charset, *criteria):
        assert criteria[0] == "SINCE"
        return "OK", [b"1"]

    def fetch(self, number, parts):
        return "OK", [(b"1 (RFC822)", self.EMAIL)]


def test_imap_source_scores_alert_jobs_and_fetches_allowed_pages(web, laya, monkeypatch):
    routes, _ = web
    routes["https://www.linkedin.com/robots.txt"] = httpx.Response(200, text="User-agent: *\nDisallow: /")
    routes["https://open.example/jobs/9"] = httpx.Response(
        200, text="<html><title>Automation Engineer</title><body><main><p>Python automation.</p></main></body></html>",
        headers={"content-type": "text/html"})
    monkeypatch.setattr(crawl.imaplib, "IMAP4_SSL", FakeMailbox)
    monkeypatch.setattr(crawl.alerts, "alert_items", lambda message: [
        ("https://www.linkedin.com/jobs/view/123", {"source_url": "https://www.linkedin.com/jobs/view/123",
         "title": "Python Developer", "company": "Acme", "description": "Python role from the alert.", "warnings": []}),
        ("https://open.example/jobs/9", {"source_url": "https://open.example/jobs/9", "title": "from the email",
         "description": "snippet", "warnings": []}),
    ])
    write_sources([{"kind": "imap", "label": "job-alerts", "max_runs_per_day": 9}])
    monkeypatch.delenv("IMAP_USER", raising=False)
    assert crawl.run() == 1 and "IMAP_USER and IMAP_PASSWORD" in db.last_run()["errors"][0]
    monkeypatch.setenv("IMAP_USER", "me@example.com")
    monkeypatch.setenv("IMAP_PASSWORD", "app-password")
    assert crawl.run() == 0
    assert titles() == ["Automation Engineer", "Python Developer"]  # LinkedIn from the email, the other page fetched
    assert crawl.run() == 0 and db.last_run()["new_items"] == 0  # the next run adds nothing


def test_imap_source_with_an_unknown_label_is_an_error(web, laya, monkeypatch):
    monkeypatch.setattr(crawl.imaplib, "IMAP4_SSL", FakeMailbox)
    monkeypatch.setenv("IMAP_USER", "me@example.com")
    monkeypatch.setenv("IMAP_PASSWORD", "app-password")
    write_sources([{"kind": "imap", "label": "typo"}])
    assert crawl.run() == 1 and "no mail label called 'typo'" in db.last_run()["errors"][0]
