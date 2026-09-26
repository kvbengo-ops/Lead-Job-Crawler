# Automated Job Scraping Plan

Goal: set preferences once, and new jobs keep showing up on the dashboard, scored and deduplicated, without pasting links.

## Where things stand (checked 2026-09-26)

Most of the machinery already exists. What's missing is small, but one gap is critical.

| Piece | Status | Where |
|---|---|---|
| Greenhouse, Lever, RSS/Atom, and listing-page sources | Done | `app/crawl.py` |
| Manage sources from the dashboard (writes `sources.json`) | Done | `/sources`, `app/main.py` |
| ETag / Last-Modified conditional requests | Done | `app/crawl.py` `fetch` |
| Mark an item seen only after it's processed; failures retry next run | Done | `app/crawl.py` run loop |
| Run lock, per-source daily limit, one failing source doesn't stop the run | Done | `app/crawl.py` |
| Start Laya for the run, retry `pending_evaluation` items | Done | `scripts/run_crawl.ps1`, `pipeline.retry_pending` |
| Preferences: work mode, employment type, locations, salary floor, blocked words | Done | Profile page |
| Scheduled task script (catches up missed runs, never overlaps, 2 h cap) | Written, **not installed** | `scripts/register_task.ps1` |
| Per-source health (last success, last error, error count) | **Missing** | `source_state` only has ETag / run count |
| Sources that find jobs you don't already know about | **Missing** | Greenhouse/Lever only cover companies you name |

**The critical gap:** no `JobCrawler` task exists in Task Scheduler, so nothing crawls on its own today. The only runs logged were manual.

## Source strategy

Prefer structured data over page scraping, and public data over logged-in data:

1. **Public feeds and APIs** (stable, cheap, no browser)
2. **Listing pages** fetched directly (works today for OnlineJobs.ph)
3. **Paste the text** for the odd page that needs JavaScript or a login (already built)

Browser automation and logged-in sessions are left out; see "Deferred".

### Discovery sources (checked 2026-09-26)

Greenhouse and Lever only list jobs at companies you already know. These boards cover the whole market:

| Source | Format | Works today? | Terms to respect |
|---|---|---|---|
| We Work Remotely: `https://weworkremotely.com/categories/remote-programming-jobs.rss` (other categories too) | RSS | **Yes**: add as an `rss` source, no code | Link back to the posting (the app keeps `source_url`) |
| Remotive: `https://remotive.com/api/remote-jobs?category=software-dev` (also `&search=python`) | JSON API | Needs a small `remotive` source kind | **4 requests a day at most**; link back and credit Remotive; jobs arrive 24 h late |
| Remote OK: `https://remoteok.com/api` | JSON API | Needs a small `remoteok` source kind | Link back and credit Remote OK, or they suspend API access |
| OnlineJobs.ph saved search | Listing page | Yes (already configured) | robots.txt is respected |

Filtering happens in the source URL (category feeds, `search=`), not in new code. If one feed still sends too many irrelevant jobs to Laya, add a per-source keyword filter then.

## Phases

### Phase 1: Turn it on (no code, ~10 minutes)

- Register the task: `powershell -ExecutionPolicy Bypass -File scripts\register_task.ps1` (runs at 08:00, 13:00 and 18:00 by default; change them with `-Times`).
- Add the We Work Remotely programming feed on the Sources page as an RSS source, max 3 runs a day.
- Check it works: after the next scheduled time, `data\logs\crawl-<date>.log` has a run and the dashboard's Crawler card shows it.

**Done when:** a crawl runs with nobody at the keyboard and new postings appear on the dashboard.

### Phase 2: Source health (small)

A source that silently breaks (a login wall, a layout change, a dead feed) currently looks just like a source with no new jobs.

- `source_state` gains `last_ok_at`, `last_error`, `error_count` (consecutive failures) and `last_new` (new items last run), added by the existing column migration in `db.init()`.
- `crawl.py` updates them for each source on every run.
- **A listing page that yields 0 job links counts as an error.** That is what a layout change or login wall looks like.
- The Sources page shows one status per source: *Healthy, 3 new last run* / *Failing 2 runs: <error>* / *Not run yet* / *Daily limit reached*.
- The dashboard's Crawler card shows a warning when any source has failed 2 runs in a row.
- One test: a failing source raises `error_count`, a good run resets it.

**Done when:** breaking a source's URL shows up on the dashboard after two runs.

### Phase 3: Discovery APIs (small)

- Add source kinds `remotive` and `remoteok` to `crawl.py`, each mapping the JSON fields to an opportunity (title, company, description, location, salary, date, URL). Items go through the same normalize → dedupe → Laya path as RSS.
- `remotive` sources default to and are capped at `max_runs_per_day: 4`. Remote OK needs a browser-like User-Agent.
- Credit: the opportunity page already links to `source_url` and shows the source host (e.g. `remotive.com`), which covers link-back and attribution for personal use.
- One test per kind with a saved sample response in `tests/fixtures/`.

**Done when:** a Remotive search and the Remote OK feed each add scored jobs on a scheduled run.

### Phase 4: Notifications (small)

- When a run ends, show a Windows notification if it found jobs scoring ≥ 80 ("3 new strong matches"), or if a source crossed 2 failures. Clicking it opens the dashboard.
- Built with the WinRT toast API that ships with Windows PowerShell 5.1, called from `scripts/run_crawl.ps1`. No new dependencies.
- The threshold is a setting on the Profile page, defaulting to 80.

**Done when:** a strong match found by a scheduled run raises a notification.

## Deferred, and when to revisit

| Idea | Why not now | Revisit when |
|---|---|---|
| Build search URLs from saved keywords | Every board has its own URL format, so it's one template per board. Pasting a board's search URL already covers it | You use 5+ boards with keyword searches |
| Browser-backed sources (Playwright) | Adds ~150 MB of browsers and breaks on layout changes. Paste-the-text covers one-off pages | One specific high-value board has no feed or usable listing page |
| Logged-in sessions | Sessions expire, and sites like LinkedIn forbid logged-in scraping in their terms | Probably never; use that site's own job alerts instead |
| Hosted, always-on collection | Laya and Ollama would have to be hosted too, not just the crawler | Missing jobs while the PC is off actually costs you opportunities |

## Requirements

- **The PC is on and you're logged in** at the scheduled times. The task doesn't store a password, so it only runs while you're logged on; runs missed while the PC was off or asleep run when it comes back.
- **Laya installed.** `run_crawl.ps1` starts it for the run. If it can't start, items are saved as `pending_evaluation` and scored on a later run.
- **Ollama is optional**: it's only used for AI drafts, not for crawling.
- **The project's `.venv`** with `requirements.txt` installed. No new Python packages are needed for any phase.
- **Internet access** and respect for each source's terms: Remotive at most 4 times a day, link-back and credit for Remotive and Remote OK, robots.txt for pages.
