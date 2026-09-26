# Phase 1 TODO

Goal: the "First implementation target" in [PLAN.md](PLAN.md). Paste text or a URL, save it to SQLite, dedupe by URL, ask Laya, score it, show it in a dashboard, and export a Markdown draft. Also measure Laya's accuracy on hand-labeled postings. At the end, run the same pipeline automatically at least three times a day over a short list of sources.

**Owners:** **Claude** = Laya integration, scoring, evaluation. **Codex** = storage, extraction, dashboard, drafts, tests. **You** = labeling and review.

Each owner only edits their own files. Changes to the shared contract below are agreed first and recorded here.

> **Handover, 2026-09-26: Claude has taken over Codex's tasks X2–X7 and all Codex-owned files** (`app/db.py`, `extract.py`, `normalize.py`, `pipeline.py`, `main.py`, `drafts.py`, `crawl.py`, `tests/`, `requirements.txt`), at the user's request. Codex: please don't edit these files; propose changes in the Coordination section instead.

## Layout

```text
app/
  db.py            Codex   SQLite schema and queries (stdlib sqlite3)
  extract.py       Codex   fetch one URL, JSON-LD then basic HTML extraction
  normalize.py     Codex   cleaning and canonical URL
  drafts.py        Codex   Markdown draft templates
  main.py          Codex   FastAPI app and server-rendered pages (port 8100)
  crawl.py         Codex   scheduled crawl entry point (python -m app.crawl)
  templates/       Codex   Jinja2 HTML
  laya_client.py   Claude  one or two requests per opportunity to Laya
  score.py         Claude  hard filters and Phase 1 score
profile.json       Claude  skills, filters, Laya questions (user edits values)
sources.json       You     feeds and job-board APIs to crawl (format set by Codex in X7)
eval/
  labels.jsonl     You     hand-labeled postings
  run_eval.py      Claude  Laya accuracy report
scripts/
  laya.ps1           Claude  shared helper: check and start Laya
  start.ps1          Claude  start Laya and the app
  run_crawl.ps1      Claude  make sure Laya is up, then run one crawl
  register_task.ps1  Claude  create the Windows scheduled task
tests/             Codex   pytest (except the two files below)
  test_laya_client.py  Claude
  test_score.py        Claude
```

## Shared contract

**Opportunity** (a dict, the shape stored in `opportunities`):

```python
{
  "id": int, "source_url": str | None, "canonical_url": str | None,
  "title": str | None, "company": str | None, "description": str,
  "location": str | None, "remote": bool | None,
  "salary_min": float | None, "salary_max": float | None, "currency": str | None,
  "contact_email": str | None, "published_at": str | None,   # ISO 8601
  "raw_text": str, "extracted_by": dict[str, str],           # field -> "jsonld" | "html" | "user"
  "warnings": list[str], "duplicate_of": int | None,
  "status": str,  # new | pending_evaluation | shortlisted | ignored
}
```

**Laya client** (`laya_client.py`):

```python
evaluate(opportunity: dict, profile: dict) -> {
  "answers": {name: {"type": "choice" | "score" | "noul",
                     "value": str | float | bool, "answer_confidence": float,
                     "probabilities": dict}},
  "model": str, "question_version": str, "error": str | None,
}
```

`value` is the chosen label for `choice`, a 0–1 number for `score` (0 = first level, 1 = last level), and `True`/`False` for `noul` (yes/no). *Added by Claude:* the `type` key, and an optional `"state": "description"` on a question in `profile.json`. Questions with that setting are sent in a second request that contains only the description (see C1 notes).

**Scoring** (`score.py`):

```python
score(opportunity: dict, evaluation: dict, profile: dict) -> {
  "passed": bool, "reject_reasons": list[str], "score": float,  # 0-100
  "components": dict[str, float], "reasons": list[str], "needs_review": bool,
}
```

*Changes recorded during the takeover (2026-09-26):*
- `extracted_by` values can also be `greenhouse`, `lever` or `rss` for items read from those sources.
- Your label corrections are stored on the opportunity (`labels` column), not in `evaluations`, so re-evaluating doesn't erase them. `evaluations` also stores `passed`, `needs_review` and `reject_reasons`.
- `pipeline.process(text=, url=, op=)` also accepts a ready-made record (used by the crawler). A URL that is already stored returns the original's id without being fetched again.
- Rejected items keep their status. They are hidden by default, and the list's "Show rejected" filter shows them.

`main.py` and `crawl.py` both call `extract` → `normalize` → dedupe → `evaluate` → `score`, and store the results in `evaluations`. Put that sequence in one shared function (for example `pipeline.process()` in Codex's code) so the dashboard and the crawler can't drift apart. When `evaluate` returns an `error`, the opportunity is saved with status `pending_evaluation` instead of being scored.

## Tasks

### Codex

*Tasks X1–X7 were finished by Claude after the handover. All 93 tests pass (`.venv\Scripts\python.exe -m pytest`).*

- [x] **X1. Project setup and schema.** `requirements.txt`, `db.py`, SQLite tables, `data/app.db`, and `.gitignore` are implemented.
  *Done when:* `python -c "import app.db as d; d.init()"` creates the file, and a test inserts and reads back one opportunity.
- [x] **X2. Extraction.** `extract.from_text(text)` and `extract.from_url(url)`. For URLs: check robots.txt, send an honest User-Agent, 10 s timeout, parse schema.org `JobPosting` JSON-LD first, then fall back to title, meta description and main text. Missing fields stay `None` and add a warning. Record `extracted_by` per field.
  *Done when:* tests pass on 3 saved HTML fixtures (with JSON-LD, without JSON-LD, and a non-job page).
- [x] **X3. Normalization and dedup.** Trim whitespace, lowercase emails, ISO dates. Canonical URL: lowercase host, strip `utm_*` and other tracking parameters, drop fragments and trailing slashes. Set `duplicate_of` when the canonical URL already exists.
  *Done when:* there are table-driven tests for canonical URLs and a duplicate insert test.
- [x] **X4. Dashboard.** `main.py` on port 8100. Pages:
  - **Add:** paste text or a URL.
  - **List:** sorted by score, with filters for type, status and needs-review.
  - **Detail:** extracted fields, source link, Laya answers with confidence, score breakdown and reasons, buttons to shortlist, ignore or correct a label.
  Label corrections are saved to `evaluations`. Until C1 and C3 land, use a stub `evaluate`.
  *Done when:* you can add a posting and see it end to end with the stub.
- [x] **X5. Markdown drafts.** `drafts.py` provides job-application and lead-email templates, dashboard editing, SQLite storage, and Markdown download. Nothing is sent anywhere.
  *Done when:* a draft can be created, edited, saved and downloaded.
- [x] **X6. Tests pass.** `pytest` is green. The Laya client is mocked in tests, so no Laya server is needed.
- [x] **X7. Crawl command.** `python -m app.crawl` handles:
  - **Sources:** reads `sources.json` (define its format: `url`, `kind` = `rss` | `greenhouse` | `lever` | `page`, `max_runs_per_day`). Fetches each source that is due, and skips sources whose `max_runs_per_day` is already used up today.
  - **Incremental fetching:** uses `ETag` / `If-Modified-Since` and feed item IDs, so repeat runs only process new items.
  - **Processing:** runs every new item through the shared pipeline, and retries every `pending_evaluation` item first.
  - **Lock:** `data/crawl.lock`, so a second run exits immediately while one is in progress. A lock older than 2 hours counts as stale.
  - **Run log:** a new `crawl_runs` table (start, end, sources fetched, new items, duplicates, pending, errors). The dashboard shows the last run and a "new since last visit" count.
  - **Exit code:** non-zero if the run failed.

  *Done when:* two runs in a row over 2 real feeds add items only on the first run, a run with Laya stopped leaves items `pending_evaluation` and the next run evaluates them, and a concurrent second run exits on the lock. These are covered by tests with mocked HTTP and Laya.

### Claude

- [x] **C1. Laya client.** `laya_client.py` using httpx to `http://127.0.0.1:8000/v1/systemone`. All questions go in one request. Cut the description to Laya's 50,000-character limit, report `answer_confidence`, and use a 30 s timeout. When Laya is down or errors, return `error` instead of raising.
  *Done when:* it works against the running Laya server, and a mocked test covers the error path.
  *Notes:*
  - Laya reads only 512 tokens, so descriptions are cut at 2,000 characters.
  - Starting the text with a "Title: … / Location: …" header helps the type question (10/10 correct on samples) but makes relevance useless. With the header, a nurse job scored 0.79 relevance for a Python profile; with the description alone it scored 0.09.
  - Questions can therefore choose `"state": "description"`. Relevance uses it, which costs one extra request of about 0.7 s.
- [x] **C2. Profile.** *Restored after Codex's stub overwrote it. Verified end to end through `pipeline.process` with live Laya.* `profile.json` with skills, services, locations, salary range, blocked words, a confidence threshold, and the Laya question set (type, relevance, work mode) with a `question_version`.
  *Done when:* C1 builds its request from it, and editing a question needs no code change.
- [x] **C3. Scoring.** `score.py` with:
  - **Hard filters:** spam or irrelevant type, blocked words, excluded location, salary below the minimum, duplicate.
  - **Score:** Laya relevance adjusted by skill-keyword overlap, salary fit and location fit, with a reason for each.
  - **`needs_review`:** set when any `answer_confidence` is below the threshold.
  *Done when:* table-driven tests cover each filter and the needs-review rule.
- [x] **C4. Evaluation script.** `eval/run_eval.py` reads `labels.jsonl`, calls Laya, and prints accuracy per question, a confusion matrix for type, and accuracy above and below the confidence threshold.
  *Done when:* it runs on your labels and the results are written to `eval/report.md`.
  *Status:* tested on 10 sample postings (type 10/10, relevance ranking 95%). It also reports relevance ranking, because ranking is what the score uses relevance for. The real run waits on U2.
- [x] **C5. Start script.** `scripts/start.ps1` starts Laya (`LAYA_DEVICE=cuda`, `LAYA_MODELS=english`) if it isn't running, waits for `/health`, then starts the app on 8100.
  *Done when:* one command brings up both services from a fresh terminal.
- [x] **C6. Review Codex's work.** *Replaced by the takeover: every Coordination item below was fixed as part of X2–X7.*
- [x] **C7. Scheduled crawl.** *Done and tested against the current `crawl.py`.* A test task registered with three triggers, ran with result 0, logged, wrote a `crawl_runs` row, and was removed with `-Unregister`. `run_crawl.ps1` stops Laya afterwards only if it started Laya itself; pass `-KeepLaya` to keep it running. Two scripts:
  - `scripts/run_crawl.ps1`: starts Laya the same way as `start.ps1` if it isn't running, waits up to 5 minutes for `/health`, then runs `python -m app.crawl`. If Laya never comes up it still runs, so items are stored as pending. Appends output to `data/logs/crawl-YYYY-MM-DD.log`.
  - `scripts/register_task.ps1`: registers a Windows scheduled task named `JobCrawler` that runs `run_crawl.ps1` at 08:00, 13:00 and 18:00, with the times as parameters. Settings: run only when you're logged on (no stored password), start as soon as possible after a missed run, don't start a new instance if one is already running, and stop after 2 hours. It also takes an `-Unregister` switch.

  *Done when:* the task appears in Task Scheduler with three triggers, a manual "Run" produces a log and a `crawl_runs` row, and `-Unregister` removes it.

### You

- [ ] **U1. Fill in the profile through the dashboard** at `/profile` at http://127.0.0.1:8100/profile: your real skills, services, work modes, locations and minimum yearly salary. It updates only those fields in `profile.json`; Laya's questions and the score weights are kept.
- [ ] **U2. Label 30–50 real postings.** Easiest: open postings in the dashboard, set type, work mode and relevant under "Your labels", then click **Export labels** and save the file as `eval/labels.jsonl`. Or write the file by hand, one JSON object per line: `{"title": "...", "text": "...", "type": "job", "relevant": true, "work_mode": "remote"}`. Only `text` is required. Include `title`, and `company` or `location` when the posting has them, because real requests include them. Mix jobs, freelance work, leads and junk, and include a few hard ones.
- [ ] **U4. Pick 2–5 sources** for `sources.json` (after X7 defines the format). Prefer official feeds and APIs, such as a few companies' Greenhouse or Lever boards and an RSS job feed. Check each site's terms.
- [ ] **U5. Register the schedule** by running `scripts/register_task.ps1` once (after C7), then let it run for a day and check the dashboard.
- [ ] **U3. Phase 1 exit check.** Read `eval/report.md` and decide: is Laya good enough to rank by, or do we fine-tune it or swap questions for rules before Phase 2?

## AI drafts with Ollama (added 2026-09-26)

Pulled forward from Phase 3. On each opportunity's page, **AI job application** and **AI lead email** send the posting and the profile's factual fields (name, headline, skills, services, experience, work modes, preferred locations) to the local `qwen3:4b` through Ollama (`POST http://127.0.0.1:11434/api/chat`). The result is saved to `drafts` with status `ai_generated` and opened in the editor. Nothing is sent anywhere; the template drafts remain as the fallback. Code: `app/ollama_client.py`, tests: `tests/test_ollama.py` (Ollama is mocked).

- **Reasoning can't be turned off:** the installed `qwen3:4b` is a thinking-only model. The prompt asks it to think briefly, which brought a draft from "never finished in 4.5 minutes" down to about 70-210 s on this PC. A non-thinking model would be several times faster: set `OLLAMA_MODEL` (for example to `qwen3:4b-instruct` after `ollama pull qwen3:4b-instruct`). Not tested here.
- **Rules aren't followed perfectly:** in testing, a job application still claimed "available to start immediately". Every AI draft is marked for review in the editor.
## Findings from the first real run (2026-09-26)

A real crawl of the We Work Remotely programming feed (25 jobs) with the live Laya ran without errors, but it also showed:

- **Relevance ranking looks weak on real postings.** For the placeholder Python/FastAPI profile, "Chief Architect" scored highest (93.2) and React and .NET jobs scored about 85. 19 of 25 were flagged "needs review" for low confidence. Real descriptions are 4,000–11,000 characters and often open with company boilerplate, but Laya reads only about the first 512 tokens. U2 and U3 will measure this properly; possible fixes include cutting boilerplate or choosing which part of the text to send.
- **Speed:** about 5 s per job (two Laya requests each) with the PC low on RAM. A board with 300 jobs takes about 30 minutes on its first run; later runs only evaluate new jobs.

## Coordination

*All resolved on 2026-09-26 by the takeover.* Found while Claude and Codex were working at the same time (2026-09-25):

1. **`profile.json` is Claude's file (C2).** It was overwritten with a stub and has since been restored. Claude's version is compatible with `drafts.py` (same `name` and `skills` keys). Please don't replace it; to change it, record the change here.
2. **`requirements.txt` is missing `python-multipart`.** FastAPI needs it for the Add form, and the app logs "Form data requires python-multipart" at startup.
3. **`main.py` inserts titles and descriptions into HTML without escaping.** Scraped pages can carry `<script>`. Use Jinja2 autoescaping (planned in `templates/`) or `html.escape`.
4. **`main.py` shows `Â·` instead of `·`.** The file's encoding got mangled. Save it as UTF-8, or use `&middot;`.
5. **`pipeline.py`:**
   - It wraps `evaluate` and `score` in `except Exception`, which hides real bugs as "pending". `evaluate` never raises for server problems, so that `try` can go.
   - Store `passed`, `reject_reasons` and `needs_review` from `score()`. The dashboard filters need them.
   - Read `profile.json` from the project root (not the current directory) with `encoding="utf-8-sig"`. Windows editors often save a BOM, and plain `utf-8` then fails to parse.
   - *Confirmed end to end:* spam that `score()` rejects is still listed as `new` with a score (48.7 in the test), because `passed` isn't stored or used for status.
6. **`extract.from_text` puts the whole pasted text into `title`.** Use the first non-empty line as the title (capped at about 150 characters) and the rest as the description. Otherwise the dashboard shows the entire posting as its title and Laya reads the text twice.
7. **`db.py` opens connections with `with connect() as con:`.** For `sqlite3`, that commits but never closes the connection. Close it explicitly, for example with `contextlib.closing` plus a commit.
8. **`crawl.py`** doesn't yet have the X7 lock, incremental fetching, `crawl_runs`, or pending retries. It also stops at the first failing source instead of continuing.

## Order

1. **In parallel:** X1, C1, C2, U2.
2. **Next:** X2, X3, C3 (C3 needs C2). X4 can start with the stub `evaluate`.
3. **Then:** X4 wired to the real C1 and C3, C4 (needs U2), X5, C5.
4. **Scheduling:** X7 (needs the shared pipeline from X4), then U4, then C7 (needs X7 and C5), then U5.
5. **Finally:** X6, C6, U3.
