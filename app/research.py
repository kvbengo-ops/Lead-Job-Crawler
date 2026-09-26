"""Weekly source research helper.

The model call is intentionally small and injectable; URL validation is done by
the caller so invented URLs never become source suggestions.
"""
from __future__ import annotations

import json
import os
import re

import httpx


class ResearchError(RuntimeError):
    pass


def _urls(text: str) -> list[dict]:
    try:
        data = json.loads(text[text.find("["):text.rfind("]") + 1])
    except (ValueError, TypeError) as e:
        raise ResearchError("The research model did not return a URL list.") from e
    return [x for x in data if isinstance(x, dict) and x.get("url")]


def research_sources(profile: dict, client: httpx.Client | None = None) -> list[dict]:
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        raise ResearchError("ANTHROPIC_API_KEY is not set.")
    about = ", ".join(str(x) for x in (profile.get("skills") or profile.get("services") or []))
    body = {"model": os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-5"), "max_tokens": 1200,
            "tools": [{"type": "web_search_20250305", "name": "web_search"}],
            "messages": [{"role": "user", "content":
                f"Find useful job boards, subreddits, and freelance communities for these skills: {about}. "
                "Use web search. Return only a JSON array of objects with url, name, and why, and only URLs you visited."}]}
    own = client is None
    client = client or httpx.Client(timeout=60)
    try:
        response = client.post("https://api.anthropic.com/v1/messages", headers={
            "x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json"}, json=body)
    except httpx.HTTPError as e:
        raise ResearchError(f"Research request failed: {e}") from e
    finally:
        if own:
            client.close()
    if response.status_code != 200:
        raise ResearchError(f"Research API returned HTTP {response.status_code}.")
    data = response.json()
    text = "\n".join(str(x.get("text", "")) for x in data.get("content", []) if isinstance(x, dict))
    return _urls(text)
