import pytest

from app.score import score

PROFILE = {
    "skills": ["python", "c++", "node.js"],
    "services": ["automation"],
    "work_modes": ["remote"],
    "preferred_locations": ["manila"],
    "excluded_locations": ["india"],
    "min_salary": 50000,
    "currency": "USD",
    "blocked_words": ["crypto"],
    "confidence_threshold": 0.6,
    "skills_for_full_match": 3,
    "score_weights": {"relevance": 60, "skills": 20, "salary": 10, "location": 10},
    "reject_types": ["spam", "irrelevant"],
}


def ans(value, conf=0.9):
    return {"value": value, "answer_confidence": conf, "probabilities": {}}


def ev(type_="job", relevance=1.0, work_mode="unclear", conf=0.9, error=None):
    answers = {} if error else {"type": ans(type_, conf), "relevance": ans(relevance), "work_mode": ans(work_mode)}
    return {"answers": answers, "model": "m", "question_version": "v", "error": error}


def op(**kw):
    base = {"title": "Engineer", "company": "Acme", "description": "Python, C++ and Node.js automation",
            "location": None, "remote": False, "salary_min": None, "salary_max": None, "currency": None,
            "duplicate_of": None}
    return {**base, **kw}


@pytest.mark.parametrize("opportunity, evaluation, reason", [
    (op(duplicate_of=7), ev(), "Duplicate of #7"),
    (op(), ev(type_="spam"), "Laya type is spam"),
    (op(), ev(type_="irrelevant"), "Laya type is irrelevant"),
    (op(description="Crypto trading bot"), ev(), "Blocked word: crypto"),
    (op(location="Bangalore, India"), ev(), "Excluded location: india"),
    (op(salary_max=40000, currency="USD"), ev(), "Salary up to 40000 is below your minimum of 50000"),
])
def test_hard_filters_reject(opportunity, evaluation, reason):
    result = score(opportunity, evaluation, PROFILE)
    assert result["passed"] is False
    assert reason in result["reject_reasons"]


@pytest.mark.parametrize("opportunity", [
    op(location="Indianapolis, IN"),        # "india" must not match inside another word
    op(description="Cryptography research"),  # "crypto" must not match inside another word
    op(salary_max=40000, currency="EUR"),   # different currency: not compared
])
def test_no_false_rejections(opportunity):
    assert score(opportunity, ev(), PROFILE)["passed"] is True


def test_unconfident_spam_is_kept_for_review():
    result = score(op(), ev(type_="spam", conf=0.4), PROFILE)
    assert result["passed"] is True
    assert result["needs_review"] is True


def test_needs_review_below_threshold_only():
    assert score(op(), ev(conf=0.59), PROFILE)["needs_review"] is True
    assert score(op(), ev(conf=0.6), PROFILE)["needs_review"] is False


def test_laya_error_needs_review_and_scores_neutral_relevance():
    result = score(op(), ev(error="Laya not reachable"), PROFILE)
    assert result["needs_review"] is True
    assert result["components"]["relevance"] == 0.5
    assert any("Laya evaluation failed" in r for r in result["reasons"])


def test_perfect_match_scores_100():
    profile = {**PROFILE, "employment_types": ["full_time"]}
    result = score(op(remote=True, salary_max=90000, currency="USD", employment_types=["full_time"]),
                   ev(relevance=1.0), profile)
    assert result["passed"] is True
    assert result["components"] == {"salary": 1.0, "relevance": 1.0, "skills": 1.0, "location": 1.0,
                                    "employment": 1.0}
    assert result["score"] == 100.0


def test_weighted_average():
    # relevance 0.5*60 + skills 1*20 + salary unknown 0.5*10 + location unclear 0.5*10 + employment no pref 0.5*10
    # = 65 of 110 weight
    result = score(op(), ev(relevance=0.5), PROFILE)
    assert result["score"] == round(65 / 110 * 100, 1)


@pytest.mark.parametrize("wanted, offered, value, reason", [
    ([], ["full_time"], 0.5, "No employment type preference set"),
    (["full_time"], [], 0.5, "Employment type not stated"),
    (["full_time", "flexible_hours"], ["part_time", "flexible_hours"], 1.0,
     "Employment type flexible hours matches your preference"),
    (["full_time"], ["part_time"], 0.0, "Employment type part time is not in your preferences (full time)"),
])
def test_employment_type(wanted, offered, value, reason):
    result = score(op(employment_types=offered), ev(), {**PROFILE, "employment_types": wanted})
    assert result["components"]["employment"] == value
    assert reason in result["reasons"]
    assert result["passed"] is True  # a mismatch lowers the score but never rejects


def test_profile_weights_without_employment_still_count_it():
    profile = {**PROFILE, "employment_types": ["full_time"], "score_weights": {"relevance": 60}}
    matched = score(op(employment_types=["full_time"]), ev(relevance=0.5), profile)["score"]
    mismatched = score(op(employment_types=["part_time"]), ev(relevance=0.5), profile)["score"]
    assert matched > mismatched


def test_remote_false_means_unknown_not_onsite():
    assert score(op(remote=False), ev(work_mode="unclear"), PROFILE)["components"]["location"] == 0.5


def test_work_mode_from_laya():
    assert score(op(), ev(work_mode="remote"), PROFILE)["components"]["location"] == 1.0
    assert score(op(), ev(work_mode="onsite"), PROFILE)["components"]["location"] == 0.0


def test_preferred_location_wins():
    assert score(op(location="Manila"), ev(work_mode="onsite"), PROFILE)["components"]["location"] == 1.0


def test_skill_matching_partial():
    result = score(op(description="We use Python"), ev(), PROFILE)
    assert result["components"]["skills"] == round(1 / 3, 3)
    assert "Skills matched: python" in result["reasons"]


def test_empty_profile_does_not_crash():
    result = score(op(), ev(), {})
    assert result["passed"] is True
    assert 0 <= result["score"] <= 100


def test_unsure_work_mode_is_noted_but_not_flagged_for_review():
    evaluation = ev()
    evaluation["answers"]["work_mode"]["answer_confidence"] = 0.3
    result = score(op(remote=False), evaluation, PROFILE)
    assert not result["needs_review"] and result["components"]["location"] == 0.5
    assert any("Low Laya confidence on work_mode" in r for r in result["reasons"])
    evaluation["answers"]["relevance"]["answer_confidence"] = 0.3
    assert score(op(remote=False), evaluation, PROFILE)["needs_review"]
