"""Ask the local Laya server the profile's questions about one opportunity.

Questions sharing a state layout go in one request, so an evaluation is one or two requests.

Answer shape per question (see TODO.md "Shared contract"):
  {"type": "choice" | "score" | "noul", "value": ..., "answer_confidence": float, "probabilities": dict}
  choice -> value is the chosen label
  score  -> value is the expected level scaled to 0..1 (0 = first level, 1 = last level)
  noul   -> value is True when P(true) >= 0.5
"""
import os

import httpx

# 127.0.0.1, not localhost: Windows tries IPv6 ::1 first and Laya binds IPv4 only (~2s stall per call).
LAYA_URL = os.environ.get("LAYA_URL", "http://127.0.0.1:8000")
TIMEOUT = float(os.environ.get("LAYA_TIMEOUT", "30"))
# Laya reads at most 512 tokens of state plus question, so text past ~2,000 characters is dropped
# by the model anyway. Cutting here keeps requests small and far under the server's 50,000 limit.
MAX_DESCRIPTION_CHARS = 2000


def _description(opportunity: dict) -> str:
    text = (opportunity.get("description") or opportunity.get("raw_text") or "").strip()
    if len(text) > MAX_DESCRIPTION_CHARS:
        text = text[:MAX_DESCRIPTION_CHARS].rsplit(" ", 1)[0] + " ..."
    return text


def build_state(opportunity: dict, layout: str = "full") -> str:
    """The text Laya reads. A question picks its layout with "state" in profile.json:

    "full" (default): key facts first, then the description, so model-side truncation only loses
      description text. Best for classifying the opportunity type.
    "description": the description alone. The "Title: ..." header makes Laya rate unrelated jobs as
      relevant (a nurse job scored 0.79 relevance for a Python profile with it, 0.09 without), so
      relevance questions use this.
    """
    if layout == "description" and _description(opportunity):
        return _description(opportunity)
    lines = []
    for label, key in (("Title", "title"), ("Company", "company"), ("Location", "location")):
        if opportunity.get(key):
            lines.append(f"{label}: {opportunity[key]}")
    if opportunity.get("remote") is True:
        lines.append("Remote: yes")
    lo, hi = opportunity.get("salary_min"), opportunity.get("salary_max")
    if lo is not None or hi is not None:
        span = " - ".join(f"{v:g}" for v in (lo, hi) if v is not None)
        lines.append(f"Salary: {span} {opportunity.get('currency') or ''}".rstrip())
    description = _description(opportunity)
    if description:
        lines += ["", description] if lines else [description]
    return "\n".join(lines)


def build_questions(profile: dict) -> dict:
    """Profile questions with {skills} and {services} filled in from the profile."""
    fill = {
        "{skills}": ", ".join(profile.get("skills", [])) or "none listed",
        "{services}": ", ".join(profile.get("services", [])) or "none listed",
    }
    questions = {}
    for name, q in profile["questions"].items():
        text = q["instructions"]
        for placeholder, value in fill.items():
            text = text.replace(placeholder, value)
        questions[name] = {**q, "instructions": text}
    return questions


def _post(client: httpx.Client, state: str, questions: dict) -> tuple[dict | None, str | None]:
    """One /v1/systemone call. Returns (response data, None) or (None, error message)."""
    headers = {"Authorization": "Bearer " + os.environ["LAYA_API_KEY"]} if os.environ.get("LAYA_API_KEY") else {}
    # Plain-text state: a dict would be JSON-serialized, spending tokens on quotes and escaped newlines.
    try:
        response = client.post(LAYA_URL + "/v1/systemone", json={"state": state, "questions": questions},
                               headers=headers)
    except httpx.TimeoutException:
        return None, f"Laya timed out after {TIMEOUT:g}s"
    except httpx.HTTPError as e:
        return None, f"Laya not reachable at {LAYA_URL}: {e}"
    try:
        data = response.json()
    except ValueError:
        data = None
    if response.status_code != 200:
        detail = data.get("detail") if isinstance(data, dict) else response.text[:200]
        return None, f"Laya HTTP {response.status_code}: {detail}"
    return data, None


def parse_answer(raw: dict) -> dict:
    kind = raw["type"]
    if kind == "choice":
        value, probs = raw["choice"], raw["probabilities"]
    elif kind == "score":
        levels = len(raw["probabilities"])
        value = raw["score"] / (levels - 1) if levels > 1 else 0.0
        probs = raw["probabilities"]
    elif kind == "noul":
        value = raw["noul"] >= 0.5
        probs = {"false": round(1 - raw["noul"], 4), "true": raw["noul"]}
    else:
        raise ValueError(f"unknown answer type {kind!r}")
    return {"type": kind, "value": value, "answer_confidence": raw["answer_confidence"], "probabilities": probs}


def evaluate(opportunity: dict, profile: dict, client: httpx.Client | None = None) -> dict:
    """Never raises for server trouble: failures come back in "error" with empty answers."""
    result = {"answers": {}, "model": None, "question_version": profile.get("question_version"), "error": None}
    if not profile.get("questions"):
        result["error"] = "profile.json has no Laya questions"
        return result
    # One request per state layout (normally at most two), with every question for that layout in it.
    groups: dict[str, dict] = {}
    for name, q in build_questions(profile).items():
        q = dict(q)
        groups.setdefault(q.pop("state", "full"), {})[name] = q

    own_client = client is None
    client = client or httpx.Client(timeout=TIMEOUT)
    answers, model = {}, None
    try:
        for layout, questions in groups.items():
            data, error = _post(client, build_state(opportunity, layout), questions)
            if error:
                result["error"] = error
                return result
            try:
                answers.update({name: parse_answer(data["answers"][name]) for name in questions})
            except (KeyError, TypeError, ValueError, ZeroDivisionError) as e:
                result["error"] = f"unexpected Laya response ({type(e).__name__}: {e})"
                return result
            routing = (data.get("routing") or {}).get("model")
            model = model or (f"{data.get('model')}/{routing}" if routing else data.get("model"))
    finally:
        if own_client:
            client.close()
    result["answers"], result["model"] = answers, model
    return result
