import json

import httpx

from app import laya_client

PROFILE = {
    "skills": ["python", "sql"],
    "services": ["automation"],
    "question_version": "test.1",
    "questions": {
        "type": {"type": "choice", "instructions": "What kind?", "criteria": {"job": "a job", "spam": "junk"}},
        "relevance": {"type": "score", "instructions": "Match for {skills}; {services}?",
                      "criteria": ["none", "weak", "partial", "strong"]},
        "urgent": {"type": "noul", "instructions": "Is it urgent?"},
    },
}

OK_RESPONSE = {
    "model": "laya-rl-agent",
    "routing": {"model": "english"},
    "answers": {
        "type": {"type": "choice", "choice": "job", "probabilities": {"job": 0.9, "spam": 0.1},
                 "confidence": 0.5, "answer_confidence": 0.9},
        "relevance": {"type": "score", "score": 2.4, "probabilities": {"0": 0.1, "1": 0.1, "2": 0.1, "3": 0.7},
                      "confidence": 0.4, "answer_confidence": 0.7},
        "urgent": {"type": "noul", "noul": 0.3, "confidence": 0.7, "answer_confidence": 0.7},
    },
}


def client(handler):
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_one_request_with_filled_questions_and_facts_first():
    sent = []

    def handler(request):
        sent.append(json.loads(request.content))
        return httpx.Response(200, json=OK_RESPONSE)

    op = {"title": "Python dev", "company": "Acme", "remote": True, "salary_min": 50000, "salary_max": 70000,
          "currency": "USD", "description": "word " * 1000}
    laya_client.evaluate(op, PROFILE, client(handler))

    assert len(sent) == 1
    state = sent[0]["state"]
    assert state.startswith("Title: Python dev\nCompany: Acme\nRemote: yes\nSalary: 50000 - 70000 USD\n\n")
    assert len(state) < laya_client.MAX_DESCRIPTION_CHARS + 200
    assert state.endswith(" ...")
    assert sent[0]["questions"]["relevance"]["instructions"] == "Match for python, sql; automation?"
    assert set(sent[0]["questions"]) == {"type", "relevance", "urgent"}


def test_description_layout_questions_go_in_a_second_request():
    sent = []

    def handler(request):
        body = json.loads(request.content)
        sent.append(body)
        answers = {name: OK_RESPONSE["answers"][name] for name in body["questions"]}
        return httpx.Response(200, json={**OK_RESPONSE, "answers": answers})

    profile = {**PROFILE, "questions": {**PROFILE["questions"],
                                        "relevance": {**PROFILE["questions"]["relevance"], "state": "description"}}}
    result = laya_client.evaluate({"title": "Python dev", "description": "Build APIs"}, profile, client(handler))

    assert result["error"] is None
    assert set(result["answers"]) == {"type", "relevance", "urgent"}
    by_questions = {frozenset(b["questions"]): b for b in sent}
    assert set(by_questions) == {frozenset({"type", "urgent"}), frozenset({"relevance"})}
    assert by_questions[frozenset({"relevance"})]["state"] == "Build APIs"
    assert by_questions[frozenset({"type", "urgent"})]["state"].startswith("Title: Python dev")
    assert "state" not in by_questions[frozenset({"relevance"})]["questions"]["relevance"]


def test_description_layout_falls_back_to_full_when_no_description():
    assert laya_client.build_state({"title": "Python dev"}, "description") == "Title: Python dev"


def test_answers_are_parsed_to_contract_shape():
    result = laya_client.evaluate({"description": "x"}, PROFILE, client(lambda r: httpx.Response(200, json=OK_RESPONSE)))

    assert result["error"] is None
    assert result["model"] == "laya-rl-agent/english"
    assert result["question_version"] == "test.1"
    assert result["answers"]["type"] == {"type": "choice", "value": "job", "answer_confidence": 0.9,
                                         "probabilities": {"job": 0.9, "spam": 0.1}}
    assert abs(result["answers"]["relevance"]["value"] - 0.8) < 1e-9  # 2.4 of levels 0..3
    assert result["answers"]["urgent"]["value"] is False
    assert result["answers"]["urgent"]["probabilities"] == {"false": 0.7, "true": 0.3}


def test_server_down_returns_error_instead_of_raising():
    def handler(request):
        raise httpx.ConnectError("refused", request=request)

    result = laya_client.evaluate({"description": "x"}, PROFILE, client(handler))
    assert result["answers"] == {}
    assert "not reachable" in result["error"]


def test_timeout_returns_error():
    def handler(request):
        raise httpx.ReadTimeout("slow", request=request)

    assert "timed out" in laya_client.evaluate({"description": "x"}, PROFILE, client(handler))["error"]


def test_http_error_detail_is_reported():
    handler = lambda r: httpx.Response(422, json={"detail": "question 'type': bad criteria"})
    result = laya_client.evaluate({"description": "x"}, PROFILE, client(handler))
    assert result["error"] == "Laya HTTP 422: question 'type': bad criteria"


def test_missing_answer_is_an_error():
    partial = {**OK_RESPONSE, "answers": {"type": OK_RESPONSE["answers"]["type"]}}
    result = laya_client.evaluate({"description": "x"}, PROFILE, client(lambda r: httpx.Response(200, json=partial)))
    assert result["answers"] == {}
    assert result["error"].startswith("unexpected Laya response")


def test_profile_without_questions_is_an_error_without_calling_laya():
    def handler(request):
        raise AssertionError("Laya should not be called")

    for profile in ({}, {"questions": {}}):
        assert laya_client.evaluate({"description": "x"}, profile, client(handler))["error"] == \
            "The profile has no Laya questions"


def test_non_json_error_body():
    result = laya_client.evaluate({"description": "x"}, PROFILE, client(lambda r: httpx.Response(500, text="boom")))
    assert result["error"] == "Laya HTTP 500: boom"
