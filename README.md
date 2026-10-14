# Acme Services — Ops Automation Scripts (sanitized demo copy)

A set of Python scripts that run a small property-maintenance company's back office: watching inboxes, pulling exports from SaaS tools, crunching the data, and emailing reports. Today they run as **cron jobs and systemd user services on a single Ubuntu (WSL2) box**. There is no orchestration layer: state lives in JSON files, retries are hand-rolled, and failures show up as silence.

That makes them a good proof-of-concept candidate for durable workflows.

> **This is a scrubbed copy.** All credentials, client names, employee names, emails, IDs, and billing rates have been replaced with placeholders (`YOUR_GEMINI_API_KEY`, `Client A`, `Employee 03`, `owner@example.com`, etc.). Rates are fake round numbers. Nothing here will run against real systems without new credentials.

---

## External dependencies

| Dependency | Used for |
|---|---|
| **gog** (Google Workspace CLI) | All Gmail search/read/send and Google Sheets reads/writes, called via `subprocess` |
| **Google Gemini API** (`google-genai`, model `gemini-2.5-flash`) | Summaries, classification, narrative reports |
| **ClockShark** (time tracking SaaS) | CSV timesheet exports, downloaded with Selenium (no API) |
| **Coast** (work-order platform) | Alert emails + CSV exports delivered via pre-signed S3 links |
| **Bee** (`@beeai/cli`) | Meeting-audio transcripts from a wearable |
| **ntfy** | Push notifications to a phone |
| **SSH to a Raspberry Pi** | Plays an alert chime on the office TV kiosk |
| **SMTP** | One legacy script sends mail directly instead of via gog |

Python packages: `google-genai`, `selenium`, `pandas` (some scripts), `google-auth` / `google-api-python-client` (kanban + timesheet scripts).

---

## Scripts

### Coast work-order pipeline (long-running services + chained steps)

| Script | Trigger | What it does |
|---|---|---|
| `coast_alert_watcher.py` | systemd service, polls every 45s | Polls a bot Gmail inbox for new work-order alert emails (two different templates), dedupes **per message ID** (not thread ID — Gmail threads same-subject alerts together), then fires an SSH chime on the Pi + an ntfy push. Keeps a daily counter in a JSON file. |
| `coast_export_fetcher.py` | systemd service, polling | Watches for "Your export is ready" emails, downloads the CSV from the pre-signed S3 URL, then **invokes `coast_stats.py`**. |
| `coast_stats.py` | called by the fetcher | Pure counting/date math over the CSV; writes `coast_stats.json` for the office TV dashboard. No AI. |
| `coast_digest.py` | cron, weekdays 6:00 AM | Reads the latest Coast CSV and emails a formatted digest of open work orders, grouped by assignee. |

### Billing & client reporting

| Script | Trigger | What it does |
|---|---|---|
| `billing_summary.py` | cron, weekdays 6:05 AM | Reads month-to-date ClockShark hours, maps each job to a client + rate (some clients bill per-employee, some by task type), emails today / week / month billable totals. |
| `weekly_client_summary.py` | cron, Mondays 7:00 AM | Groups the week's ClockShark entries by client; Gemini writes a narrative summary per client; emails an HTML report. |
| `email_billable_finder.py` | cron, Mondays 7:30 AM, `--days 7` | Scans several staff mailboxes, uses Gemini to find client-facing work that may not have been billed, normalizes client names, emails a spreadsheet per person. Longest-running and most API-heavy job. |

### Timesheets & team ops

| Script | Trigger | What it does |
|---|---|---|
| `email_timesheet_2day.py` | cron, weekdays 5:00 AM | Emails the last 2 *working* days of timesheet data (Monday pulls the prior Thu + Fri). |
| `kanban_notifier.py` | cron, every 2 min | Diffs a Google Sheet kanban board against the last snapshot and emails the affected people about changes. |
| `bee_meeting_summary.py` | manual (chat command) | Pulls the last ~10 hours of wearable transcripts, Gemini summarizes the meeting, emails it and logs a row to a Google Sheet. |

### `clockshark_audit/` — timesheet audit pipeline

The most "workflow-shaped" piece: **download → generate audit → process → send**, with Selenium logins that fail intermittently.

| Script | What it does |
|---|---|
| `download_clockshark.py` / `download_clockshark_month.py` | Selenium login to ClockShark and export of the period / month CSV. |
| `generate_audit.py` | Builds the audit from the downloaded CSV. |
| `process_and_send.py` | Formats and emails results over SMTP. |
| `morning_par_audit.py` / `run_par_audit.py` / `par_audit_15day.py` | Variants of the "PAR" audit (shift-note / hours checks) over different windows; each downloads, analyzes with Gemini, and emails. |
| `weekly_par_trend.py` | Week-over-week trend report built from the audit output. |
| `send_missing_notes.py` | Daily 5 PM report of shifts missing required notes. |

---

## Why these are interesting for durable execution

- **Polling loops that must not lose state.** The two Coast services keep "already seen" message IDs in JSON files so a restart doesn't re-alert or skip alerts.
- **Multi-step chains glued together by subprocess calls.** Email arrives → download from S3 → compute stats → publish, and download → audit → send. A failure mid-chain currently just stops.
- **Flaky external steps.** Selenium logins, Gemini 429 rate limits (handled with hand-written backoff that honors `retryDelay`), and the gog CLI's keyring prompts.
- **Rate-limit isolation by design.** Cron scripts and the always-on AI agent use separate Gemini projects so overnight agent traffic doesn't starve the morning reports.
- **Business-calendar logic.** "Last two working days," Monday look-backs, and weekday-only schedules.
- **Fan-out.** The billable finder loops over several mailboxes, and one bad mailbox token shouldn't kill the whole run.

---

## Configuration

Most secrets were hardcoded in the originals and are now **inline placeholders** in the code (search for `YOUR_`). A few scripts already read from the environment. See `.env.example` for the full list.
