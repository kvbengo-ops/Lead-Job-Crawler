# Your Setup Checklist

This checklist covers the tasks that require your accounts, browser actions, or manual judgment.

## 1. Start the dashboard

Open PowerShell in the project folder:

```powershell
cd "C:\Users\Asus\Desktop\This is job Cralwer"
powershell -ExecutionPolicy Bypass -File scripts\start.ps1
```

Leave that window running, then open:

```text
http://127.0.0.1:8100
```

If Laya is not found, check that this file exists:

```text
C:\Users\Asus\Desktop\Laya\.venv\Scripts\python.exe
```

## 2. Add recurring job sources

1. Open `http://127.0.0.1:8100/sources`.
2. Add an OnlineJobs.ph page source for each useful search.
3. Add these searches, changing them to match your skills:

```text
https://www.onlinejobs.ph/jobseekers/jobsearch?jobkeyword=python
https://www.onlinejobs.ph/jobseekers/jobsearch?jobkeyword=automation
https://www.onlinejobs.ph/jobseekers/jobsearch?jobkeyword=web+developer
```

4. Add the We Work Remotely programming RSS feed:

```text
https://weworkremotely.com/categories/remote-programming-jobs.rss
```

Choose `RSS` as its source kind and use a maximum of 3 runs per day.

5. Keep only searches that produce useful jobs. Remove irrelevant sources from the Sources page.

## 3. Register automatic crawling

Run this from the project folder:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\register_task.ps1
```

The default schedule is 08:00, 13:00, and 18:00. To use different times, run for example:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\register_task.ps1 -Times 07:30,12:00,17:00,21:00
```

Confirm the task exists:

```powershell
Get-ScheduledTask -TaskName JobCrawler
```

The task runs while your Windows account is logged in. Your computer must be awake for a crawl to run.

## 4. Check the first scheduled crawl

You can run a crawl immediately instead of waiting:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\run_crawl.ps1
```

Then check:

- The dashboard's Crawler card.
- The run log in `data\logs`.
- Whether new opportunities appear in the dashboard.
- Whether any sources report errors.

## 5. Label postings for Laya evaluation

1. Open the opportunity list in the dashboard.
2. Review 30–50 real postings.
3. Open each posting's detail page.
4. Correct Laya's labels when they are wrong.
5. Include a mixture of relevant jobs, irrelevant jobs, spam, remote, onsite, freelance, and lead postings.
6. Use the dashboard's `Export labels` action.
7. Save the exported file as:

```text
eval\labels.jsonl
```

Do not manually edit the exported JSON unless the application tells you to.

## 6. Set up job-alert email collection (optional)

Create a separate Gmail account for job alerts if possible.

1. Turn on 2-Step Verification for the Gmail account.
2. Create a Gmail app password.
3. Turn on IMAP in Gmail settings.
4. Create a Gmail label named `job-alerts`.
5. Create filters that apply `job-alerts` to job-alert emails.
6. Enable saved-search alerts on LinkedIn, Indeed, and JobStreet.
7. Set the environment variables in PowerShell:

```powershell
setx IMAP_USER "you@gmail.com"
setx IMAP_PASSWORD "your-gmail-app-password"
```

Close and reopen PowerShell after using `setx`.

The default IMAP host is `imap.gmail.com`. Do not use your normal Gmail password; use the app password.

Save one or two alert emails from each board as `.eml` files for testing. Before saving them, remove your name, email address, phone number, and other personal information.

## 7. Optional weekly source research

This step uses paid API credits.

1. Obtain an Anthropic API key.
2. Set it in PowerShell:

```powershell
setx ANTHROPIC_API_KEY "your-anthropic-api-key"
```

3. Close and reopen PowerShell.
4. Run the research command when source research support is wired into the dashboard:

```powershell
.venv\Scripts\python.exe -m app.research
```

## Completion checklist

- [ ] Dashboard opens at `http://127.0.0.1:8100`.
- [ ] Three to five useful sources are configured.
- [ ] `JobCrawler` appears in Windows Task Scheduler.
- [ ] One manual crawl completes successfully.
- [ ] New jobs appear without pasting individual links.
- [ ] 30–50 postings are labeled.
- [ ] `eval\labels.jsonl` exists.
- [ ] Job-alert Gmail is configured, if needed.
- [ ] IMAP variables are set, if needed.
- [ ] Anthropic API key is set, if weekly research is desired.

