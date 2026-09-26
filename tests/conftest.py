"""Shared test setup: every test gets its own database, profile, lock, and sources file, and no real network."""
import json

import httpx
import pytest

from app import crawl, db, extract, pipeline

PROFILE = {
    "name": "Test User",
    "skills": ["python", "sql"],
    "services": ["automation"],
    "work_modes": ["remote"],
    "preferred_locations": [],
    "excluded_locations": [],
    "min_salary": None,
    "currency": "USD",
    "blocked_words": [],
    "confidence_threshold": 0.6,
    "reject_types": ["spam", "irrelevant"],
    "question_version": "test.1",
    "questions": {
        "type": {"type": "choice", "instructions": "What kind?",
                 "criteria": {"job": "a job", "lead": "a lead", "spam": "junk", "irrelevant": "not an opportunity"}},
        "relevance": {"type": "score", "state": "description", "instructions": "Match for {skills}?",
                      "criteria": ["none", "weak", "partial", "strong"]},
        "work_mode": {"type": "choice", "instructions": "Where?",
                      "criteria": {"remote": "remote", "onsite": "office", "unclear": "not stated"}},
    },
}


class FakeLaya:
    """Stands in for app.laya_client.evaluate. Text containing "spam" is classified as spam."""

    def __init__(self):
        self.down = False
        self.calls = 0

    def __call__(self, op, profile):
        self.calls += 1
        if self.down:
            return {"answers": {}, "model": None, "question_version": "test.1", "error": "Laya not reachable"}
        text = f"{op.get('title') or ''} {op.get('description') or ''}".lower()
        kind = "spam" if "spam" in text else "job"
        answer = lambda value, kind_="choice": {"type": kind_, "value": value, "answer_confidence": 0.9,
                                                "probabilities": {}}
        return {"answers": {"type": answer(kind), "relevance": answer(0.8 if "python" in text else 0.2, "score"),
                            "work_mode": answer("remote" if "remote" in text else "unclear")},
                "model": "fake/english", "question_version": "test.1", "error": None}


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "app.db")
    profile_path = tmp_path / "profile.json"
    profile_path.write_text(json.dumps(PROFILE), encoding="utf-8")
    monkeypatch.setattr(pipeline, "PROFILE_PATH", profile_path)
    monkeypatch.setattr(crawl, "SOURCES_PATH", tmp_path / "sources.json")
    monkeypatch.setattr(crawl, "LOCK_PATH", tmp_path / "crawl.lock")
    monkeypatch.setattr(crawl, "SAME_HOST_DELAY", 0)
    monkeypatch.setattr(extract, "_robots", {})
    db.init()
    # No test may reach the real network unless it installs routes with the `web` fixture.
    install_web(monkeypatch, {})


@pytest.fixture
def laya(monkeypatch):
    fake = FakeLaya()
    monkeypatch.setattr(pipeline, "evaluate", fake)
    return fake


def install_web(monkeypatch, routes: dict):
    """routes: URL -> httpx.Response, or a callable(request) -> httpx.Response. Unknown URLs get 404."""
    requests = []

    def handler(request: httpx.Request):
        requests.append(request)
        url = str(request.url)
        route = routes.get(url)
        if route is None:
            return httpx.Response(404, text="not found")
        return route(request) if callable(route) else route

    def make_client():
        return httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True,
                            headers={"User-Agent": extract.USER_AGENT})

    monkeypatch.setattr(extract, "make_client", make_client)
    monkeypatch.setattr(crawl, "make_client", make_client)
    return requests


@pytest.fixture
def web(monkeypatch):
    routes: dict = {}
    requests = install_web(monkeypatch, routes)
    return routes, requests
