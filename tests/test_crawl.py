import json
import os
import time

import httpx
import pytest

from app import crawl, db

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
