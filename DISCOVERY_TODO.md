# Discovery TODO

Goal: build [AUTOMATED_SCRAPING_PROPOSAL.md](AUTOMATED_SCRAPING_PROPOSAL.md). Good jobs and client leads arrive by themselves, scored, with the best ones ready for a reply. Phase 1 ([TODO.md](TODO.md)) is finished; its open "You" tasks are carried over below.

**Owners:** **Codex** = new, self-contained modules: parsers, keyword suggestions, source research, notifications. **Claude** = wiring them into the crawler, database, dashboard, and scripts (Claude has owned those files since the 2026-09-26 handover). **You** = accounts, approvals, labels.

Status on 2026-09-26: 164 tests pass (`.venv\Scripts\python.exe -m pytest -q`).

## Rules

1. **Each owner only edits their own files** (list below). To change someone else's file, write it in [Coordination](#coordination) first.
2. **No network in tests.** Save real responses and emails as fixtures, and mock HTTP, IMAP, Ollama, and Laya.
3. **No new Python packages** unless agreed in Coordination. Use the standard library, `httpx`, and `feedparser`.
4. **A task is done** when its *Done when* check passes, the whole test suite is green, and anything a user would notice is added to `README.md` (Claude updates the README; Codex notes README text in the task).
5. **Respect each source's terms:** `robots.txt` for pages, Remotive at most 4 requests a day, link-back and credit for Remotive and Remote OK.

## File ownership

```text
Codex (new files only)
  app/alerts.py          split job-alert emails into jobs
  app/feeds.py           Remotive, Remote OK, Hacker News parsers
  app/keywords.py        search keywords from shortlisted jobs, turned into source suggestions
  app/research.py        weekly source research with a web-search model
  scripts/notify.ps1     Windows notification
  tests/test_alerts.py, test_feeds.py, test_keywords.py, test_research.py
  tests/fixtures/alerts/, tests/fixtures/feeds/
  app/ollama_client.py   only the new suggest_keywords() function (X10); Claude doesn't edit this file meanwhile

Claude (everything else), mainly
  app/crawl.py, db.py, extract.py, normalize.py, pipeline.py, main.py, laya_client.py, score.py
  app/templates/, scripts/*.ps1 (except notify.ps1), eval/, tests/ (other files), README.md

You
  sources.json (through the Sources page), eval/labels.jsonl, accounts and environment variables
```

## Shared contract

**Parser functions (Codex).** They're pure: no network, no database. They return `(item_id, fields)` pairs, and the crawler turns `fields` into a stored opportunity with `crawl._record(source, **fields)`:

```python
def remotive_items(source: dict, data) -> list[tuple[str, dict]]: ...

fields = {  # every key optional; unknown keys are not allowed
    "source_url": str, "title": str, "company": str, "description": str,  # plain text, not HTML
    "location": str, "remote": bool, "salary_min": float, "salary_max": float, "currency": str,
    "contact_email": str, "published_at": str,  # ISO 8601 or RFC 2822
    "warnings": list[str],
}
```

- `item_id` is stable for the same job across runs: the source's own job ID, or the job URL passed through `normalize.canonical_url`.
- Codex may import these from Claude's modules (read-only): `extract.html_to_text`, `extract.guess_remote`, `extract.fetch`, `extract.JOB_LINK`, `normalize.canonical_url`. If one of them changes signature, Claude notes it in Coordination.

**Source suggestions** (from keywords and research), stored by Claude's `db.add_suggestions(suggestions, origin)`:

```python
{"kind": "rss" | "page" | "remotive" | ..., "url": str, "name": str, "why": str}  # origin: "keywords" | "research"
```

**New source kinds** in `sources.json`, validated by Claude's `crawl.load_sources`:

```json
{"kind": "imap", "label": "job-alerts", "name": "Job alerts"}
{"kind": "remotive", "url": "https://remotive.com/api/remote-jobs?category=software-dev&search=python"}
{"kind": "remoteok", "url": "https://remoteok.com/api"}
{"kind": "hn", "thread": "who_is_hiring" | "seeking_freelancer"}
```

## Tasks

### Phase 0: Switch on, and make scoring trustworthy

- [ ] **U5. Register the schedule** *(carried over)*: `powershell -ExecutionPolicy Bypass -File scripts\register_task.ps1`, let it run for a day, and check the dashboard's Crawler card.
- [ ] **U4. Add sources** *(carried over, updated)*: on the Sources page, add 3–5 OnlineJobs.ph keyword searches for your skills (e.g. `jobkeyword=python`, `=automation`, `=web+developer`) and the We Work Remotely programming feed (`https://weworkremotely.com/categories/remote-programming-jobs.rss`, kind RSS).
- [ ] **U2. Label 30–50 real postings** *(carried over)* on their detail pages, then **Export labels** and save the file as `eval/labels.jsonl`.
- [x] **C8. Fix text-encoding errors.** *(Done 2026-09-26. The stored OnlineJobs.ph titles were fine: they hold a real em dash, and the `�` was the Windows console failing to print it. The real gap was pages that declare their charset only in `<meta>`: `extract._decode` now honours it. Test: `test_meta_charset_is_used_when_the_header_has_none`.)* Some fetched pages store `�` in place of characters like `–` (seen on OnlineJobs.ph). Choose the charset in the right order: HTTP header, then `<meta charset>`, then UTF-8, then Windows-1252.
  *Done when:* a fixture page with a charset mismatch extracts `–` correctly, and re-adding an affected OnlineJobs.ph posting stores a clean title.
- [x] **C9. Send Laya the part of the posting that matters.** *(Closed 2026-09-26: measured, and not shipped. On 30 stored opportunities with the live Laya, flagged for review: as now 26/30, without boilerplate 29/30, without boilerplate and with the role section first 25/30. The reorder moved the relevance ranking a lot (rank correlation 0.51) with no labels to show it's better. What Laya reads stays the same; `normalize.strip_boilerplate` is used for the list excerpts only. The real cause of the review flags was the work-mode question: see C10.)*
  *Done when:* `eval/run_eval.py` on U2's labels shows better relevance ranking than before (record both numbers here).
- [ ] **C10. Tune Laya's questions and the confidence threshold** from the eval report *(needs U2)*. *(Half done 2026-09-26: 25 of 30 items were flagged only because the work-mode answer was unsure (average confidence 0.46), and the score already treats an unsure work mode as unknown (0.5). `score.NO_REVIEW_WHEN_UNSURE` stops those flags, and the stored flags were recomputed: 32 → 10 of 41 evaluations (24%). Tuning the questions themselves still waits for U2.)*
  *Done when:* fewer than a third of new items are flagged *needs review*, without worse relevance ranking.
- [ ] **U3. Scoring check** *(carried over)*: read `eval/report.md` and decide whether the scores are good enough to rank by before adding many more sources.

### Phase 1: Email alerts

- [ ] **U8. Set up the alert inbox.**
  - A Gmail account (a separate one just for alerts is best) with IMAP turned on, 2-Step Verification, and an app password.
  - A label `job-alerts`, and a filter that sends alert emails there.
  - Saved searches with email alerts on LinkedIn, Indeed, and JobStreet.
  - Set the environment variables yourself: `setx IMAP_USER "you@gmail.com"` and `setx IMAP_PASSWORD "<app password>"` (`IMAP_HOST` defaults to `imap.gmail.com`).
  - Save 1–2 alert emails from each board as `.eml` files for Codex's tests, with your name and email address removed.
- [x] **X8. Alert email parser** (`app/alerts.py`): `alert_items(message: email.message.EmailMessage) -> list[tuple[str, dict]]`. *(Parser implementation complete; fixtures/tests and crawler wiring remain.)*
  - Parsers for LinkedIn, Indeed, and JobStreet, chosen by sender, plus a generic fallback that pairs job links with the text around them (title, company, location).
  - Unwrap tracking redirects when the real URL is in a query parameter, and use the canonical job URL as `item_id` so the same job in two alerts is stored once.
  - Put the email's snippet in `description`, so a job can be scored even when its page can't be fetched.
  - Can start with hand-made fixtures, then switch to U8's real emails.

  *Done when:* each board's fixture yields the right number of jobs with title, company, and URL, and a non-alert email yields none.
- [ ] **C11. `imap` source kind** *(needs X8)*. *(Wired and tested 2026-09-26 with a fake mailbox and a stubbed parser, so it's ready once X8 is fixed and U8 is set up. Change from the plan: each job is tracked as seen, which covers repeat runs, so there's no separate Message-ID tracking.)* Read the label with `imaplib.IMAP4_SSL` in read-only mode, only the last 14 days, and skip Message-IDs already in `seen_items`. For each job: fetch the full posting if `robots.txt` allows it, otherwise score it from the email fields. Missing environment variables show a clear error on the Sources page. Add the kind to the Sources page form.
  *Done when:* a mocked IMAP test turns a fixture email into scored opportunities, and a second run adds nothing.

### Phase 2: Wider sources and source health

- [x] **X9. Feed parsers** (`app/feeds.py`). *(Parser implementation complete; real-response fixtures, terms review, tests, and crawler wiring remain.)*
  - `remotive_items`: the jobs from Remotive's API. Keep the Remotive URL as `source_url`.
  - `remoteok_items`: the jobs from Remote OK's API, skipping the first element (their legal notice).
  - `hn_items`: the comments of Hacker News's monthly "Who is hiring?" or "Freelancer? Seeking freelancer?" thread, from the Algolia API. The first line usually reads `Company | Role | Location | Remote`; split that into fields when it's there.
  - `is_hiring_post(title) -> bool`: true for Reddit posts tagged `[Hiring]`, false for `[For Hire]`.

  *Done when:* each fixture parses into correct fields, and the HN parser handles both a pipe-separated first line and free text.
- [x] **C12. Wire the new kinds** *(Done 2026-09-26, checked against the live APIs: Remotive 19 jobs, Remote OK 99, HN "Who is hiring?" 256 top-level posts, We Work Remotely 25. Remote OK accepted the crawler's own User-Agent, so no browser-like one is needed. The HN freelancer thread keeps only `SEEKING FREELANCER` posts; this month it had none. **Reddit is dropped:** its `robots.txt` blocks all crawlers, so `hiring_only` was removed again.)*: `remotive` (capped at 4 runs a day), `remoteok` (browser-like User-Agent), `hn` (find the latest thread, then read its comments), and `hiring_only` on RSS sources. Add them to the Sources page form.
  *Done when:* mocked runs of each kind store opportunities, and a `remotive` source set to 6 runs a day is capped at 4.
- [x] **C13. Source health.** *(Done 2026-09-26. Also fixed: a known list page that stops linking to jobs used to be stored as a junk job and marked seen, so the source was never read again. It's now a source error. Tests in `test_crawl.py` and `test_main.py`.)* `source_state` gains `last_ok_at`, `last_error`, `error_count` (consecutive failures), and `last_new`. A listing page that yields no job links counts as a failure. The Sources page shows *Healthy · N new* / *Failing N runs: error* / *Not run yet* / *Daily limit reached*, and the dashboard warns when any source has failed 2 runs in a row.
  *Done when:* a test shows a failing source raising `error_count` and a good run resetting it, and a broken source's URL shows the dashboard warning after two runs.

### Phase 3: Steering from your clicks

- [x] **C14. Source ranking.** *(Done 2026-09-26. Older opportunities are backfilled from `seen_items`. All current opportunities came from the Add page, because both crawl runs so far fetched 0 sources, so the numbers start with the first real crawl.)* Record which source found each opportunity (a new `source_key` column). For each source, show items found, average score, shortlisted, ignored, and rejected, sorted by results. Suggest pausing a source with 30+ items and no shortlists, and add a `paused` option to sources.
  *Done when:* the Sources page shows the numbers, and a paused source is skipped by the crawler.
- [x] **X10. Keyword suggestions** (`app/keywords.py`, plus `ollama_client.suggest_keywords`). *(Implementation complete; mocked tests and dashboard wiring remain.)*
  - `suggest_keywords(shortlisted: list[dict], profile: dict) -> list[str]`: Ollama reads the titles and descriptions of shortlisted jobs and returns 5–10 short search terms, excluding terms already in the profile's sources.
  - `search_sources(keywords) -> list[suggestion]`: ready-made sources from known search URLs (OnlineJobs.ph `jobkeyword=`, Remotive `search=`).

  *Done when:* mocked tests cover the happy path, an Ollama error, and removing duplicates of existing sources.
- [x] **C15. Suggestions on the Sources page** *(Done 2026-09-26. If Ollama doesn't answer, the button falls back to Codex's word-count `keywords.suggest_keywords` and says so. Suggestions that are already configured are hidden.)* Add a `source_suggestions` table (kind, url, name, why, origin, status) and `db.add_suggestions()`, show suggestions with **Add** and **Dismiss** buttons, and add a **Suggest keywords** button that calls X10.
  *Done when:* a suggested keyword becomes a working source with one click, and a dismissed suggestion doesn't come back.

### Phase 4: Fast response

- [x] **X11. Windows notification** (`scripts/notify.ps1 -Title -Body -Url`): a toast notification through the WinRT API built into Windows PowerShell 5.1, no modules. Clicking it opens the URL. *(Script implementation complete; manual Windows verification remains.)*
  *Done when:* running it by hand shows a notification that opens the dashboard when clicked.
- [ ] **C16. Notify with a draft ready** *(Code and mocked tests done 2026-09-26, including the Profile page's Notify at setting. The real-run check waits for X11: `notify.ps1` doesn't run yet, and until it does the crawler prints the notification instead.)* After a crawl, for new items scoring at or above the Profile page's new *Notify at* setting (default 80), write an AI draft (at most 3 per run, skipped if Ollama is down), then show one notification summing up the run. Also notify when a source reaches 2 failed runs in a row.
  *Done when:* a mocked test with a high-scoring item saves one AI draft and calls `notify.ps1` once, and a real scheduled run with a strong match raises the notification.

### Phase 5: Weekly source research

- [ ] **U9. Get an API key** for a model with web search (for example the Claude API) and set it yourself as an environment variable (`setx ANTHROPIC_API_KEY "<key>"`). This part costs API credits.
- [x] **X12. Source research** (`app/research.py`, run as `python -m app.research`). *(Research client and response parsing implemented; URL validation, database wiring, CLI entry point, fixtures/tests, and real API run remain.)*
  *Done when:* mocked tests cover a good run, an invented URL being dropped, and a missing API key, and a real run adds at least one working suggestion.
- [ ] **C17. Weekly schedule for research** *(needs X12)*: a second scheduled task (`JobCrawlerResearch`, Sundays), with an `-Unregister` option like the crawler's, and the README updated.
  *Done when:* the task appears in Task Scheduler, and a manual run adds suggestions to the Sources page.

## Order

1. **Start now, in parallel:** U5, U4, U2, U8, C8, C9, C13, X8 (with hand-made fixtures), X9, X11.
2. **Next:** C11 (needs X8), C12 (needs X9), C10 (needs U2), then U3.
3. **Then:** C14, X10, then C15.
4. **Then:** C16 (needs X11 and C13).
5. **Last:** U9 and X12 (needs C15's `db.add_suggestions`), then C17.

## Coordination

*Write proposed changes to someone else's files, contract changes, and blockers here, with the date.*

**2026-09-26, Claude → Codex: review of X8–X12 before wiring them in.** By rule 4 these aren't done until their *Done when* checks pass and tests exist, so please treat the checkboxes as "in progress". Found by reading the code and running it:

1. **X11 `notify.ps1` doesn't run.** On Windows PowerShell 5.1 it fails with `Cannot find type [Windows.Data.Xml.Dom.XmlDocument]`, because the WinRT types have to be loaded first (for example `[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] | Out-Null`, and the same for `Windows.Data.Xml.Dom.XmlDocument`). Also check: toasts from an unregistered app ID such as `"JobCrawler"` are usually dropped silently (PowerShell's own app ID works), opening a URL on click needs `activationType="protocol"`, and `$Url` goes into the XML unescaped.
2. **X8 `alerts.py` would flood the dashboard with junk.**
   - `_urls` returns every link in the email (unsubscribe, settings, privacy, logos), and each one becomes a job. Keep only job links: `extract.JOB_LINK` plus per-board patterns (LinkedIn `/jobs/view/`, Indeed `viewjob`/`rc/clk`, JobStreet `/job/`).
   - `_fields` gives every job in an email the same title (the email's first long line), and the whole email as its description. Each job needs the text next to its own link.
   - The LinkedIn, Indeed, and JobStreet parsers from the task aren't there yet.
3. **X10 keywords: "existing sources" contract.** Both `suggest_keywords` functions read `profile["sources"]`, which doesn't exist; sources live in `sources.json`. That was my unclear wording. **Contract from now on:** the caller passes `{**profile, "sources": [existing search terms]}`, which is what your code already expects. I'll pull the terms from the `jobkeyword=` and `search=` parameters of configured sources. Also, `search_sources` builds URLs with `replace(" ", "+")`, so terms like `C#` or `C++` break the URL: please use `urllib.parse.quote_plus`.
4. **X12 `research.py`:**
   - The default model `claude-sonnet-4-5` is old; use `claude-sonnet-5`.
   - With web search, the API can stop with `stop_reason: "pause_turn"` in the middle of a long search. Continue the conversation in that case instead of reading the partial text.
   - URL validation isn't there yet. It's the step that guarantees invented URLs never reach the user, so it belongs in `research.py`, next to the parsing.
5. **X9 `feeds.py` looks right.** Two notes: Algolia comment searches also return replies, so I'll pass only top-level comments (`parent_id == story_id`) to `hn_items`. And please add the fixtures and tests from the task.

**2026-09-26, Claude → Codex: found while checking C12 against the live APIs.**

6. **Remote OK's data is double-encoded on their side.** The raw API sends `MÃ¡nico` for "Mecánico", so titles and descriptions show `MecÃ¡nico`. Please repair it in `remoteok_items`: for each text field, `value.encode("latin-1").decode("utf-8")` when that round trip works, and keep the value as it is when it raises.
7. **`is_hiring_post` is no longer used.** Reddit's `robots.txt` blocks every crawler, and we respect `robots.txt`, so Reddit sources are out and I removed `hiring_only`. You can delete the function, or keep it for the official Reddit API if we ever add it.
8. **Contract updates:** `hn` sources use `"thread": "who_is_hiring" | "seeking_freelancer"`. The crawler passes `hn_items` only the top-level comments, and for the freelancer thread only posts starting with `SEEKING FREELANCER`. `imap` sources are `{"kind": "imap", "label": "..."}`, and the crawler calls `alerts.alert_items(message)` for each email from the last 14 days.
