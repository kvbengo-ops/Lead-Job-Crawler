import json

import httpx
import pytest
from fastapi.testclient import TestClient

from app import db, ollama_client, pipeline
from app.main import app
from app.ollama_client import OllamaError, build_messages, clean_draft, generate_draft

OP = {"title": "Python Developer", "company": "Acme", "location": "Berlin", "remote": True,
      "salary_min": 60000.0, "salary_max": 80000.0, "currency": "EUR", "contact_email": "jobs@acme.example",
      "description": "Build FastAPI services. Contact Jane Doe."}
PROFILE = {"name": "Kyle", "headline": "Python developer", "skills": ["python", "sql"], "services": ["automation"],
           "experience": "Built invoicing automation for a small agency.", "work_modes": ["remote"],
           "min_salary": 99999, "questions": {"type": {}}, "score_weights": {"relevance": 60}}


# --- the request -------------------------------------------------------------------

def test_request_shape_and_prompt_contents(ollama):
    generate_draft(OP, PROFILE, "job_application")
    assert len(ollama.requests) == 1
    request = ollama.requests[0]
    assert (request.method, str(request.url)) == ("POST", "http://127.0.0.1:11434/api/chat")
    body = json.loads(request.content)
    assert body["model"] == "qwen2.5:3b" and body["stream"] is False and body["think"] is False
    system, user = body["messages"]
    assert system["role"] == "system" and user["role"] == "user"
    for fact in ("Title: Python Developer", "Company: Acme", "Location: Berlin (remote)",
                 "Budget or salary: 60,000 to 80,000 EUR", "Contact: jobs@acme.example",
                 "Build FastAPI services. Contact Jane Doe.",
                 "Name: Kyle", "Skills: python, sql", "Services: automation",
                 "Experience: Built invoicing automation for a small agency."):
        assert fact in user["content"], fact
    # Scoring settings are not facts about the user and must not leak into an email.
    assert "99999" not in user["content"] and "score_weights" not in user["content"]
    for rule in ("Do not invent experience", "prices", "technologies", "clients", "facts about the company",
                 "Output only the draft"):
        assert rule in system["content"], rule


def test_modes_ask_for_different_drafts():
    job = build_messages(OP, PROFILE, "job_application")[1]["content"]
    lead = build_messages(OP, PROFILE, "lead_email")[1]["content"]
    assert "job application email" in job and "first-contact email" in lead
    with pytest.raises(ValueError):
        build_messages(OP, PROFILE, "poem")


def test_missing_facts_are_marked_not_stated():
    user = build_messages({"description": ""}, {}, "lead_email")[1]["content"]
    assert "Title: not stated" in user and "Budget or salary: not stated" in user and "Contact: not stated" in user
    assert "Use placeholders for all personal details" in user


def test_long_description_is_shortened():
    user = build_messages({"description": "word " * 5000}, PROFILE, "job_application")[1]["content"]
    assert "[description shortened]" in user and len(user) < ollama_client.MAX_DESCRIPTION_CHARS + 2000


# --- the answer --------------------------------------------------------------------

@pytest.mark.parametrize("raw, expected", [
    ("Subject: Hi\n\nBody", "Subject: Hi\n\nBody"),
    ("I should be careful here...\n</think>\n\nSubject: Hi\n\nBody", "Subject: Hi\n\nBody"),  # leaked reasoning
    ("<think>plan</think>Subject: Hi", "Subject: Hi"),
    ("```\nSubject: Hi\n\nBody\n```", "Subject: Hi\n\nBody"),
    ("  \n Subject: Hi \n", "Subject: Hi"),
])
def test_clean_draft(raw, expected):
    assert clean_draft(raw) == expected


def test_returns_clean_draft(ollama):
    ollama.reply = {"message": {"content": "reasoning</think>\n\nSubject: Application\n\nHello"}, "done": True}
    assert generate_draft(OP, PROFILE, "job_application") == "Subject: Application\n\nHello"


@pytest.mark.parametrize("status, reply, message", [
    (404, {"error": "model 'qwen2.5:3b' not found"}, 'Run "ollama pull qwen2.5:3b"'),
    (500, {"error": "out of memory"}, "Ollama returned HTTP 500: out of memory"),
    (200, {"message": {"content": "   "}, "done_reason": "stop"}, "empty answer"),
    (200, {"message": {"content": "only thinking</think>"}, "done_reason": "length"}, f"ran out of its {ollama_client.MAX_TOKENS}-token budget"),
    (200, {"unexpected": True}, "empty answer"),
])
def test_ollama_errors_are_explained(ollama, status, reply, message):
    ollama.status, ollama.reply = status, reply
    with pytest.raises(OllamaError, match=message.replace("(", r"\(").replace('"', '"')):
        generate_draft(OP, PROFILE, "lead_email")


def test_offline_ollama():
    # conftest makes Ollama unreachable by default
    with pytest.raises(OllamaError, match="isn't running at http://127.0.0.1:11434"):
        generate_draft(OP, PROFILE, "lead_email")


def test_timeout(monkeypatch):
    def slow(request):
        raise httpx.ReadTimeout("too slow", request=request)
    monkeypatch.setattr(ollama_client, "make_client", lambda: httpx.Client(transport=httpx.MockTransport(slow)))
    with pytest.raises(OllamaError, match=f"didn't finish within {ollama_client.TIMEOUT:g} seconds"):
        generate_draft(OP, PROFILE, "lead_email")


# --- the dashboard -------------------------------------------------------------------

@pytest.fixture
def client(laya):
    return TestClient(app)


def add(text):
    oid, _ = pipeline.process(text=text)
    return oid


def test_ai_draft_is_saved_and_opened_for_editing(client, ollama):
    oid = add("Python Developer\nBuild FastAPI services.")
    page = client.get(f"/opportunities/{oid}").text
    assert f'action="/opportunities/{oid}/ai-draft/job_application"' in page
    assert f'href="/opportunities/{oid}/draft/job_application"' in page  # template fallback is still there

    r = client.post(f"/opportunities/{oid}/ai-draft/job_application", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].endswith("?saved=ai")
    did = int(r.headers["location"].split("/")[2].split("?")[0])
    draft = db.get_draft(did)
    assert (draft["kind"], draft["status"], draft["content"]) == (
        "job_application", "ai_generated", "Subject: Hi\n\nHello there.\n\nBest,\nTest User")

    editor = client.get(r.headers["location"]).text
    assert "Generated by qwen2.5:3b and saved" in editor and "<textarea" in editor
    client.post(f"/drafts/{did}", data={"content": "Edited by me"})
    assert client.get(f"/drafts/{did}.md").text == "Edited by me"
    assert db.get_draft(did)["status"] == "ai_generated"
    assert '<span class="chip">AI</span>' in client.get(f"/opportunities/{oid}").text


def test_lead_email_mode(client, ollama):
    oid = add("Rosa's Kitchen\nWe take orders on paper and want to automate it.")
    client.post(f"/opportunities/{oid}/ai-draft/lead_email", follow_redirects=False)
    assert "first-contact email" in json.loads(ollama.requests[0].content)["messages"][1]["content"]


def test_offline_ollama_shows_error_and_saves_nothing(client):
    oid = add("Python Developer\nBuild FastAPI services.")
    r = client.post(f"/opportunities/{oid}/ai-draft/job_application", follow_redirects=False)
    assert r.status_code == 503
    assert "No AI draft was created. Ollama isn&#39;t running" in r.text
    assert "job application template" in r.text  # the fallback is offered on the same page
    assert db.list_drafts(oid) == []


def test_ai_draft_output_is_escaped_in_editor(client, ollama):
    ollama.reply = {"message": {"content": "Subject: <script>alert(1)</script>"}, "done": True}
    oid = add("Python Developer\nBuild FastAPI services.")
    r = client.post(f"/opportunities/{oid}/ai-draft/lead_email")
    assert "<script>alert(1)" not in r.text and "&lt;script&gt;alert(1)" in r.text


def test_unknown_kind_and_opportunity(client, ollama):
    oid = add("Python Developer\nBuild FastAPI services.")
    assert client.post(f"/opportunities/{oid}/ai-draft/poem").status_code == 404
    assert client.post("/opportunities/9999/ai-draft/lead_email").status_code == 404
    assert ollama.requests == []


def test_profile_experience_is_saved(client):
    client.post("/profile", data={"experience": "Built invoicing automation.", "confidence_threshold": "0.6"})
    assert pipeline.load_profile()["experience"] == "Built invoicing automation."
    assert "Built invoicing automation." in client.get("/profile").text


def test_think_briefly_is_only_sent_when_thinking_is_on():
    # With think off, "think briefly" made qwen3 write reasoning into the draft until it timed out.
    assert ollama_client.THINK is False and "Think briefly" not in ollama_client.SYSTEM_PROMPT
