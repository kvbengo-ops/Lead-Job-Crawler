"""Local Ollama model (POST /api/chat): draft job applications and lead emails, and read a resume into profile
fields. Drafts are only returned as text for the user to edit; nothing is ever sent.
"""
from __future__ import annotations

import json
import os
import re

import httpx

# 127.0.0.1, not localhost: Windows tries IPv6 first, which can add seconds per call.
OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://127.0.0.1:11434").rstrip("/")
MODEL = os.environ.get("OLLAMA_MODEL", "qwen2.5:3b")
# Measured on this PC with reasoning on: ~800 tokens and ~85 s per draft at ~8-10
# tokens/s, so the cap leaves room for long reasoning and the timeout covers the whole cap.
TIMEOUT = float(os.environ.get("OLLAMA_TIMEOUT", "180"))
# Drafting and factual extraction do not need a long reasoning trace. Keeping this
# off makes Qwen3 responsive on a 4 GB GPU; set OLLAMA_THINK=true when testing a
# difficult prompt where extra reasoning is worth the delay.
THINK = os.environ.get("OLLAMA_THINK", "false").lower() in {"1", "true", "yes", "on"}
MAX_TOKENS = int(os.environ.get("OLLAMA_MAX_TOKENS", "1536"))
CONTEXT = int(os.environ.get("OLLAMA_NUM_CTX", "4096"))
MAX_DESCRIPTION_CHARS = 4000
MAX_RESUME_CHARS_DRAFT = 6000     # resume text added to draft prompts
MAX_RESUME_CHARS_ANALYSIS = 12000  # resume text read when filling in the profile (fits the 8k context)
KINDS = ("job_application", "lead_email")
# Profile keys the model sees. Scoring settings (questions, weights, thresholds, salary floor) are left out:
# they are not facts about the user, and a salary floor must not end up in an email.
PROFILE_KEYS = ("name", "headline", "skills", "services", "experience", "work_modes", "preferred_locations")
# Only sent when THINK is on: then it matters (without it qwen3:4b reasoned for 2,000+ tokens, 4+ minutes here).
# With THINK off it backfires: the model writes that "brief" reasoning into the answer until the token cap,
# which ran past the 180 s timeout and pushed the prompt out of the 4k context.
THINK_BRIEFLY = "\n\nThink briefly: plan in a few short sentences at most, then answer." if THINK else ""

SYSTEM_PROMPT = f"""You write drafts of emails for one person, described in PROFILE (and RESUME, when given), about one opportunity, described in OPPORTUNITY. The person will review and edit the draft before sending it themselves.

Rules:
- Use only facts stated in PROFILE, RESUME, and OPPORTUNITY.
- Do not invent experience, years of experience, employers, clients, projects, results, numbers, prices, rates, budgets, availability, technologies, tools, certifications, or facts about the company.
- Mention a skill, service, or technology only if it appears in PROFILE or RESUME.
- Describe the person's experience only as PROFILE or RESUME describes it. Do not add scale, seniority, outcomes, or adjectives such as "extensive" or "scalable".
- Do not claim the person has used a technology from the description unless PROFILE or RESUME lists it.
- When a useful detail is missing, write a short placeholder in square brackets instead, for example [add a relevant project].
- Do not mention salary, rates, or prices unless OPPORTUNITY states them, and never propose a number.
- Write in plain, friendly, professional English. No exaggeration, no pressure tactics.
- Output only the draft: a first line starting with "Subject:", a blank line, then the body, ending with the sign-off and the person's name. No commentary, notes, explanations, alternatives, or Markdown code fences.{THINK_BRIEFLY}"""

TASKS = {
    "job_application": (
        "Write a job application email for this opportunity, about 150 to 250 words. "
        "Greet the contact person by name only if a contact name is given; otherwise use \"Hello\". "
        "Connect the requirements in the description to the skills and experience in PROFILE and RESUME, "
        "and use a placeholder wherever they have nothing relevant."),
    "lead_email": (
        "Write a short first-contact email, about 90 to 150 words, offering the services in PROFILE to this "
        "business. Refer only to needs the description states. End with one low-pressure question, such as "
        "whether a short call would be useful."),
}

RESUME_PROMPT = f"""Copy details from the RESUME into JSON. Copy words from the resume; don't rewrite or judge them. Leave a field empty if the resume doesn't have it.

- name: the person's name.
- headline: their current or most recent job title.
- skills: the skills, tools, and technologies the resume lists (at most 25).
- services: up to 6 short labels for the kinds of work they have done, such as "backend development".
- experience: up to 6 lines copied from the work experience section, one role or achievement per line.
- location: the city or country the resume gives for them.

Answer with only this JSON object:
{{"name": "", "headline": "", "skills": [], "services": [], "experience": "", "location": ""}}"""
# Keep this prompt short and about copying. A stricter version ("summarize exactly as stated, add no adjectives
# or numbers") made qwen3:4b reason about its own rules for 1,600+ words without answering (7+ minutes).
# No THINK_BRIEFLY here, and format="json" in analyze_resume: with think off, qwen3 wrote its reasoning into the
# answer until it hit the token cap (~3 minutes, nothing saved). JSON mode makes the first token the object.


class OllamaError(Exception):
    """A user-facing explanation of why Ollama produced nothing usable."""


def make_client() -> httpx.Client:
    # Tests replace this to inject a mock transport.
    return httpx.Client(timeout=httpx.Timeout(TIMEOUT, connect=5))


def _money(op: dict) -> str:
    lo, hi, cur = op.get("salary_min"), op.get("salary_max"), op.get("currency") or ""
    if lo is None and hi is None:
        return "not stated"
    if lo is not None and hi is not None and lo != hi:
        return f"{lo:,.0f} to {hi:,.0f} {cur}".strip()
    return f"{(lo if lo is not None else hi):,.0f} {cur}".strip()


def _shorten(text: str, limit: int, note: str) -> str:
    text = (text or "").strip()
    if len(text) > limit:
        text = text[:limit].rsplit(" ", 1)[0] + f" [{note}]"
    return text


def build_messages(op: dict, profile: dict, kind: str, resume: str = "") -> list[dict]:
    if kind not in KINDS:
        raise ValueError(f"unknown draft kind {kind!r}")
    location = op.get("location") or "not stated"
    if op.get("remote") is True:
        location += " (remote)"
    description = _shorten(op.get("description") or op.get("raw_text") or "", MAX_DESCRIPTION_CHARS,
                           "description shortened")
    opportunity = "\n".join([
        f"Title: {op.get('title') or 'not stated'}",
        f"Company: {op.get('company') or 'not stated'}",
        f"Location: {location}",
        f"Budget or salary: {_money(op)}",
        f"Contact: {op.get('contact_email') or 'not stated'}",
        f"Description:\n{description or 'not stated'}",
    ])
    lines = []
    for key in PROFILE_KEYS:
        value = profile.get(key)
        if isinstance(value, list):
            value = ", ".join(str(v) for v in value)
        if value not in (None, ""):
            lines.append(f"{key.replace('_', ' ').capitalize()}: {value}")
    about = "\n".join(lines) or "No profile details given. Use placeholders for all personal details."
    user = f"{TASKS[kind]}\n\nPROFILE\n{about}"
    if resume and resume.strip():
        user += f"\n\nRESUME\n{_shorten(resume, MAX_RESUME_CHARS_DRAFT, 'resume shortened')}"
    user += f"\n\nOPPORTUNITY\n{opportunity}"
    return [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": user}]


def clean_draft(text: str) -> str:
    # Some model and Ollama versions leak their reasoning into the answer, ending it with </think>.
    if "</think>" in text:
        text = text.rsplit("</think>", 1)[1]
    text = text.strip()
    fence = re.fullmatch(r"```[\w-]*\n(.*?)\n?```", text, re.S)
    if fence:
        text = fence.group(1).strip()
    return text


def _chat(messages: list[dict], client: httpx.Client | None, fmt: str | None = None) -> str:
    """One /api/chat call. Returns the answer text (reasoning removed) or raises OllamaError."""
    body = {
        "model": MODEL,
        "messages": messages,
        "stream": False,
        "think": THINK,
        "options": {"temperature": 0.4, "num_predict": MAX_TOKENS, "num_ctx": CONTEXT},
    }
    if fmt:
        body["format"] = fmt
    own = client is None
    client = client or make_client()
    try:
        response = client.post(OLLAMA_URL + "/api/chat", json=body)
    except httpx.TimeoutException as e:
        raise OllamaError(f"Ollama didn't finish within {TIMEOUT:g} seconds. The first request after Ollama starts "
                          "also loads the model, so try once more; if it keeps happening, close other programs "
                          "to free memory or raise OLLAMA_TIMEOUT.") from e
    except httpx.HTTPError as e:
        raise OllamaError(f"Ollama isn't running at {OLLAMA_URL}. Start the Ollama app (or run \"ollama serve\"), "
                          "then try again.") from e
    finally:
        if own:
            client.close()

    try:
        data = response.json()
    except ValueError:
        data = {}
    if not isinstance(data, dict):
        data = {}
    error = data.get("error")
    if response.status_code == 404 and error and "not found" in error:
        raise OllamaError(f"The model {MODEL} isn't installed in Ollama. Run \"ollama pull {MODEL}\", then try again.")
    if response.status_code != 200:
        raise OllamaError(f"Ollama returned HTTP {response.status_code}: {error or response.text[:200]}")
    if data.get("done_reason") == "length":
        # Cut off by the token cap. With think off, qwen3 writes its reasoning into the answer, so a cut-off
        # answer can be pure reasoning with no </think>; it must never be returned as a draft.
        raise OllamaError(f"The model ran out of its {MAX_TOKENS}-token budget before finishing, so nothing was "
                          "saved. Try again, or raise OLLAMA_MAX_TOKENS.")
    answer = clean_draft((data.get("message") or {}).get("content") or "")
    if not answer:
        raise OllamaError("Ollama returned an empty answer. Try again.")
    return answer


def generate_draft(op: dict, profile: dict, kind: str, client: httpx.Client | None = None, resume: str = "") -> str:
    """Return the draft text, or raise OllamaError with a message for the user."""
    return _chat(build_messages(op, profile, kind, resume), client)


def _text_list(value, limit: int) -> list[str]:
    items = []
    for v in value if isinstance(value, list) else []:
        v = re.sub(r"\s+", " ", str(v)).strip(" ,;.")
        if v and v.lower() not in {i.lower() for i in items}:
            items.append(v[:60])
    return items[:limit]


def analyze_resume(text: str, client: httpx.Client | None = None) -> dict:
    """Profile suggestions read from resume text: name, headline, skills, services, experience, location.
    Raises OllamaError. The caller shows them for review; nothing here is saved."""
    messages = [{"role": "system", "content": RESUME_PROMPT},
                {"role": "user", "content": "RESUME\n" + _shorten(text, MAX_RESUME_CHARS_ANALYSIS, "resume shortened")}]
    answer = _chat(messages, client, fmt="json")
    start, end = answer.find("{"), answer.rfind("}")  # tolerate a stray sentence around the object
    try:
        data = json.loads(answer[start:end + 1] if start != -1 and end > start else answer)
    except ValueError as e:
        raise OllamaError("The model's answer wasn't valid JSON, so the profile wasn't filled in. Try again.") from e
    if not isinstance(data, dict):
        raise OllamaError("The model's answer had the wrong shape, so the profile wasn't filled in. Try again.")
    def text_field(key: str, limit: int) -> str:
        value = data.get(key) or ""
        if isinstance(value, list):  # JSON mode sometimes answers "experience" as a list of lines
            value = "\n".join(str(v).strip() for v in value if str(v).strip())
        return re.sub(r"[ \t]+", " ", str(value)).strip()[:limit]
    return {
        "name": text_field("name", 100),
        "headline": text_field("headline", 120),
        "skills": _text_list(data.get("skills"), 25),
        "services": _text_list(data.get("services"), 6),
        "experience": text_field("experience", 1500),
        "location": text_field("location", 100),
    }


def suggest_keywords(shortlisted: list[dict], profile: dict, client: httpx.Client | None = None) -> list[str]:
    """Ask Ollama for 5–10 concise search terms derived from shortlisted jobs."""
    existing = profile.get("sources", []) if isinstance(profile, dict) else []
    jobs = []
    for job in shortlisted[:30]:
        jobs.append(f"Title: {job.get('title') or ''}\nDescription: {(job.get('description') or '')[:1200]}")
    prompt = ("Return JSON only as {\"keywords\":[...]} with 5 to 10 short job-search terms. "
              "Use recurring role, skill, or service phrases from these shortlisted jobs. "
              f"Do not return terms already present in SOURCES: {existing}.\n\n" + "\n\n".join(jobs))
    answer = _chat([{"role": "system", "content": "You suggest precise job-search keywords."},
                    {"role": "user", "content": prompt}], client, fmt="json")
    try:
        data = json.loads(answer[answer.find("{"):answer.rfind("}") + 1])
    except (ValueError, TypeError) as e:
        raise OllamaError("Ollama returned invalid keyword JSON.") from e
    values = data.get("keywords") if isinstance(data, dict) else []
    out = []
    seen = {str(x).strip().lower() for x in existing}
    for value in values if isinstance(values, list) else []:
        value = re.sub(r"\s+", " ", str(value)).strip(" ,.;")
        if value and value.lower() not in seen and value.lower() not in {x.lower() for x in out}:
            out.append(value[:80])
    return out[:10]
