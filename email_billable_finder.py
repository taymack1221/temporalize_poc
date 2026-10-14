#!/usr/bin/env python3
"""
Acme Services Weekly Billable-Task Finder
Scans Owner's inbox by THREAD, uses Gemini to identify discrete tasks Owner
handled on behalf of a customer, client, or tenant, and writes a spreadsheet
for Owner to assign time to himself.

Does NOT estimate hours or dollars — only surfaces the task, date, people,
and a short summary. Owner fills in the time.

USAGE
  Weekly (default, last 7 days):  python3 email_billable_finder.py
  Whole calendar month:           python3 email_billable_finder.py --month 2026-05
  Custom range:                   python3 email_billable_finder.py --after 2026-05-01 --before 2026-06-01
  Different rolling window:        python3 email_billable_finder.py --days 14

Deploy:  /home/user/email_billable_finder.py
Cron:    30 7 * * 1  python3 /home/user/email_billable_finder.py >> /home/user/billable_finder.log 2>&1

Requires: openpyxl, google-genai  (already installed)
"""

import os
import re
import sys
import json
import time
import argparse
import subprocess
from datetime import datetime, date, timedelta

from google import genai
from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
from openpyxl.utils import get_column_letter

# ── CONFIG ────────────────────────────────────────────────────────────────────
SENDER       = "bot@example.com"   # the bot account the report email comes from
GOG_KEYRING  = "YOUR_GOG_KEYRING_PASSWORD"
GEMINI_KEY   = "YOUR_GEMINI_API_KEY"
GEMINI_MODEL = "models/gemini-2.5-flash"

# Each person whose inbox we scan. The "role" line is dropped straight into the
# Gemini prompt so it understands whose actions to surface and what their job is —
# Owner: TUNE ManagerA's and ManagerB's role lines to reflect what they actually do, since
# their billable-email pattern differs from yours. Each email account must be
# authorized in gog first (gog login for that account) or the run will 404.
# "notify" is who receives that person's spreadsheet; add Owner to keep an admin copy.
PEOPLE = {
    "tony": {
        "email": "owner@example.com",
        "name":  "Owner Smith",
        "role":  ("an owner/administrator at Acme Services (Acme Services), a "
                  "property management and maintenance company, who handles a great "
                  "deal of work by email on behalf of clients and tenants that he "
                  "never logs as billable time"),
        "notify": ["owner@example.com"],
    },
    "troy": {
        "email": "manager1@example.com",
        "name":  "Employee 02",
        "role":  ("an operations/field manager at Acme Services (Acme Services), "
                  "a property management and maintenance company, who coordinates "
                  "vendors, bids, and on-site work for clients and tenants by email "
                  "without always logging that time"),   # <-- TUNE to ManagerA's real role
        "notify": ["manager1@example.com", "owner@example.com"],
    },
    "tye": {
        "email": "manager2@example.com",
        "name":  "Employee 03",
        "role":  ("an operations/field manager at Acme Services (Acme Services), "
                  "a property management and maintenance company, who coordinates "
                  "vendors, bids, and on-site work for clients and tenants by email "
                  "without always logging that time"),   # <-- TUNE to ManagerB's real role
        "notify": ["manager2@example.com", "owner@example.com"],
    },
    "jeremy": {
        "email": "manager3@example.com",
        "name":  "Employee 07",
        "role":  ("an operations/field manager at Acme Services (Acme Services), "
                  "a property management and maintenance company, who coordinates "
                  "vendors, bids, and on-site work for clients and tenants by email "
                  "without always logging that time"),
        "notify": ["manager3@example.com", "owner@example.com"],
    },
}

DEFAULT_DAYS = 7
PAGE_SIZE    = 500          # gog caps a single page around here; we page past it
MAX_THREADS  = 5000         # hard safety cap across all pages
BATCH_CHARS  = 200_000      # chars per Gemini call; larger batches = far fewer API
                            # calls (lighter on rate/quota limits). Stays well under
                            # the model's context window.
CACHE_VERSION = "v3"        # bump when the thread-block FORMAT or cache shape changes;
                            # with a different version are ignored (prevents stale data)

OUTPUT_DIR   = "/mnt/c/AcmeReports"
NAS_COPY_DIR = "/mnt/z/billable_reports"     # optional; skipped if not mounted

VERBOSE = True

# ── HELPERS ───────────────────────────────────────────────────────────────────
def log(msg):
    print(f"[billable] {datetime.now().strftime('%H:%M:%S')}  {msg}")


def cell_text(value) -> str:
    """openpyxl can't store lists or dicts — flatten anything to clean text."""
    if value is None:
        return ""
    if isinstance(value, (list, tuple)):
        return ", ".join(cell_text(v) for v in value)
    if isinstance(value, dict):
        return ", ".join(f"{k}: {cell_text(v)}" for k, v in value.items())
    return str(value)


# ── CLIENT NAME NORMALIZATION ─────────────────────────────────────────────────
# Owner: EDIT THIS to match how you group clients for billing. Each entry is
# (Canonical Name, [lowercase substrings that should map to it]). The FIRST
# match in this list wins, so put more specific entries above more general ones
# (e.g. "Bremerton Agency" before a bare "agency"). Anything not matched is left as
# the model wrote it (just trimmed), so nothing gets lost — unmatched names just
# won't be consolidated until you add them here.
CLIENT_ALIASES = [
    ("Federal Way Centre",        ["federal way centre", "federal way", "fwc"]),
    ("Client A",           ["clienta"]),
    ("Client B",       ["clientb", "clientb2"]),
    ("Client C",     ["clientc", "clc"]),
    ("Client D",          ["clientd"]),
    ("Client E",             ["cliente"]),
    ("Client F",       ["clientf"]),
    ("Client G", ["clg", "clientg"]),
    ("Client H",                  ["clienth"]),
    ("Client I",            ["clienti"]),
    ("Client J",               ["clientj", "clientj2", "100 main st"]),
    ("Client K", ["clientk", "clk"]),
    ("Client L",             ["clientl"]),
]


# Agency offices are kept distinct by city. Add any city you operate in.
# "lkw" is treated as Lakewood. A Agency row with no recognized city is left as-is.
Agency_CITIES = ["Puyallup", "Meeker", "Everett", "Bremerton", "Bellevue",
               "Kent", "Lakewood", "Olympia", "Tacoma", "Seattle", "Spokane"]


def agency_city(low: str):
    """If a string looks like a Agency office, return its city, else None."""
    if "agency" not in low:
        return None
    if "lkw" in low:
        return "Lakewood"
    for city in Agency_CITIES:
        if city.lower() in low:
            return city
    return None


def canonical_client(raw: str) -> str:
    """Map a messy client/property string to a consistent canonical name.
    Returns the matched canonical name, or the trimmed original if no match."""
    text = cell_text(raw).strip()
    if not text:
        return ""
    low = text.lower()
    # Agency offices: keep each city separate (Agency — Puyallup, Agency — Kent, ...)
    city = agency_city(low)
    if city:
        return f"Agency — {city}"
    for canonical, needles in CLIENT_ALIASES:
        if any(n in low for n in needles):
            return canonical
    return text


def gog(args: list[str], account: str) -> str:
    env = os.environ.copy()
    env["GOG_KEYRING_PASSWORD"] = GOG_KEYRING
    result = subprocess.run(
        ["gog"] + args + ["--account", account],
        env=env, capture_output=True, text=True,
        encoding="utf-8", errors="replace",
    )
    if result.returncode != 0 and VERBOSE:
        log(f"gog error: {result.stderr.strip()}")
    return result.stdout


def search_threads(query: str, account: str) -> list[dict]:
    """Page through ALL search results via nextPageToken. Returns thread dicts
    with keys like id, date, from, subject, labels, messageCount."""
    threads, page, pages = [], None, 0
    while True:
        args = ["gmail", "search", query, "--json", "--max", str(PAGE_SIZE)]
        if page:
            args += ["--page", page]
        raw = gog(args, account)
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            log("could not parse search JSON; stopping paging")
            if VERBOSE:
                log(raw[:500])
            break
        batch = data.get("threads", []) or []
        threads.extend(batch)
        pages += 1
        page = data.get("nextPageToken")
        log(f"  page {pages}: +{len(batch)} threads (running total {len(threads)})")
        if not page or len(threads) >= MAX_THREADS:
            break

    # de-dupe by thread id, preserve order
    seen, uniq = set(), []
    for t in threads:
        tid = t.get("id")
        if tid and tid not in seen:
            seen.add(tid)
            uniq.append(t)
    return uniq[:MAX_THREADS]


def fetch_thread_text(thread_id: str, account: str) -> str:
    """Pull the full thread (all messages) as readable text, bodies included."""
    return gog(["gmail", "thread", "get", thread_id, "--full"], account)


def gather_email_blocks(query: str, tag: str, account: str,
                        refresh: bool = False):
    """Fetch all threads for the query. Returns (blocks, metas) where metas[i]
    is {id, subject, date} for the thread numbered (i+1) in the block headers.
    On-disk cache keyed by tag so a re-run skips the slow fetch."""
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    cache_path = os.path.join(OUTPUT_DIR, f"_thread_cache_{CACHE_VERSION}_{tag}.json")

    if not refresh and os.path.isfile(cache_path):
        try:
            with open(cache_path, encoding="utf-8") as f:
                data = json.load(f)
            blocks, metas = data["blocks"], data["metas"]
            log(f"loaded {len(blocks)} threads from cache ({cache_path}) — "
                f"delete that file or pass --refresh to re-fetch")
            return blocks, metas
        except Exception as e:
            log(f"cache unreadable, re-fetching: {e}")

    threads = search_threads(query, account)
    log(f"{len(threads)} unique threads to fetch")
    blocks, metas = [], []
    n = 0
    for i, t in enumerate(threads, 1):
        tid = t.get("id")
        subj = t.get("subject", "") or ""
        tdate = (t.get("date", "") or "")[:10]   # YYYY-MM-DD from the metadata
        text = fetch_thread_text(tid, account).strip()
        if text:
            n += 1
            blocks.append(
                f"\n\n===== THREAD {n} | DATE: {tdate} | {subj} =====\n{text}")
            metas.append({"id": tid, "subject": subj, "date": tdate})
        if VERBOSE and i % 50 == 0:
            log(f"  fetched {i}/{len(threads)} threads ...")
    total = sum(len(b) for b in blocks)
    log(f"collected {len(blocks)} threads, {total} chars")

    try:
        with open(cache_path, "w", encoding="utf-8") as f:
            json.dump({"blocks": blocks, "metas": metas}, f, ensure_ascii=False)
        log(f"cached threads to {cache_path}")
    except Exception as e:
        log(f"could not write thread cache (non-fatal): {e}")

    return blocks, metas


# ── GEMINI EXTRACTION ─────────────────────────────────────────────────────────
PROMPT_TEMPLATE = """You are reviewing email from the inbox of {name}, {role}.

Each block below is a full email THREAD (one or more messages). Read them and \
produce a list of discrete tasks or matters that {name} personally handled, \
coordinated, decided, or responded to ON BEHALF OF a customer, client, vendor, or \
tenant — anything that could plausibly be billable administrative work.

For each item identify:
- date: use the DATE shown in that thread's header line (format YYYY-MM-DD).
  Use ONLY the header date — ignore any other dates mentioned inside the message
  text, even if the email discusses work done on an earlier date.
- people: the names and/or emails of who {name} was dealing with (not {name})
- client: the client, building, or property involved, if identifiable; else ""
- summary: one or two plain sentences describing what occurred and what {name} did
- thread: the THREAD number shown in that thread's header line (just the integer)

RULES:
- Do NOT estimate hours, time, or dollar amounts. {name} assigns time themselves.
- Treat each THREAD as ONE item, not one row per message.
- INCLUDE only matters where {name} actually DID something on behalf of an external
  client, tenant, or vendor: coordinating, deciding, instructing, reviewing a bid or
  estimate, responding to a request, or resolving an issue. {name} must be an ACTOR,
  not merely a recipient or someone copied on the thread.
- EXCLUDE Acme Services's own internal and administrative business even if {name} was on
  the thread: payroll, taxes, and government or regulatory notices (e.g. L&I, IRS,
  ESD, DOL filings), accounts-receivable and payment-advice notifications,
  auto-generated "invoice created" notices, and bookkeeping/accounting housekeeping.
- EXCLUDE threads whose ONLY activity is internal Acme Services coordination among staff
  (e.g. Owner, ManagerA, ManagerB, Staff, reply@example.com) with NO external client,
  tenant, or vendor party in the thread. If the thread also involves an external
  party and {name} acted toward them, KEEP it.
- EXCLUDE newsletters, marketing, automated system notifications, receipts, spam,
  and personal email.
- If {name} was only notified or copied and took no client-facing action, SKIP it.
  Phrasings like "was informed", "was made aware", "was copied on" are signals to SKIP.
- Write each summary in active voice, leading with what {name} DID. One or two sentences.
- If nothing in a thread is billable-relevant, skip it.

Return ONLY a JSON array of objects with keys: date, people, client, summary, thread.
No markdown, no code fences, no commentary before or after.

EMAIL THREADS:
{emails}
"""


def batch_blocks(blocks: list[str], budget: int) -> list[list[str]]:
    batches, cur, size = [], [], 0
    for b in blocks:
        if cur and size + len(b) > budget:
            batches.append(cur)
            cur, size = [], 0
        cur.append(b)
        size += len(b)
    if cur:
        batches.append(cur)
    return batches


TRANSIENT = ("500", "502", "503", "429", "UNAVAILABLE",
             "RESOURCE_EXHAUSTED", "DEADLINE", "INTERNAL")


def gemini_generate(client, prompt: str, batch_no: int, retries: int = 6):
    """One Gemini call with backoff on transient server errors. Returns the
    response, or None if it ultimately failed."""
    delay = 4
    for attempt in range(1, retries + 1):
        try:
            return client.models.generate_content(
                model=GEMINI_MODEL, contents=prompt,
            )
        except Exception as e:
            msg = str(e)
            is_transient = any(code in msg.upper() for code in TRANSIENT)
            if attempt < retries and is_transient:
                log(f"  batch {batch_no}: {msg[:60]} — retry "
                    f"{attempt}/{retries} in {delay}s")
                time.sleep(delay)
                delay = min(delay * 2, 90)
                continue
            log(f"  batch {batch_no}: giving up after {attempt} attempt(s): {msg[:80]}")
            return None
    return None


def extract_tasks(blocks: list[str], tag: str, name: str, role: str) -> list[dict]:
    if not blocks:
        return []
    client = genai.Client(api_key=GEMINI_KEY)
    batches = batch_blocks(blocks, BATCH_CHARS)
    log(f"extracting across {len(batches)} batch(es)")

    progress_path = os.path.join(OUTPUT_DIR, f"billable_email_activity_{tag}.json")
    all_tasks, failed = [], []

    for i, batch in enumerate(batches, 1):
        prompt = PROMPT_TEMPLATE.format(name=name, role=role, emails="".join(batch))
        resp = gemini_generate(client, prompt, i)
        if resp is None:
            failed.append(i)
            continue
        raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", resp.text.strip()).strip()
        try:
            tasks = json.loads(raw)
            if isinstance(tasks, list):
                all_tasks.extend(tasks)
                log(f"  batch {i}: {len(tasks)} items")
            else:
                log(f"  batch {i}: unexpected JSON shape; skipped")
        except json.JSONDecodeError:
            log(f"  batch {i}: bad JSON; first 300 chars below")
            log(raw[:300])

        # Save progress every 10 batches so a late crash never wipes the work
        if i % 10 == 0:
            try:
                with open(progress_path, "w", encoding="utf-8") as f:
                    json.dump(all_tasks, f, indent=2, ensure_ascii=False)
            except Exception:
                pass

    if failed:
        log(f"WARNING: {len(failed)} batch(es) could not be processed: {failed}")
        log("  (threads are cached, so re-running will retry just those quickly)")

    all_tasks.sort(key=lambda t: t.get("date", ""))
    return all_tasks


# ── SPREADSHEET ───────────────────────────────────────────────────────────────
def build_spreadsheet(tasks: list[dict], label: str, file_tag: str) -> str:
    wb = Workbook()
    ws = wb.active
    ws.title = "Billable Tasks"

    headers = ["Date", "Subject", "People Involved", "Client / Property",
               "Summary of What Occurred", "Open Email",
               "Est. Billable Time", "Notes"]
    ncols = len(headers)
    last_col = get_column_letter(ncols)

    header_fill = PatternFill("solid", fgColor="1E3A5F")
    header_font = Font(bold=True, color="FFFFFF", size=11)
    link_font = Font(color="1155CC", underline="single")
    thin = Side(style="thin", color="DDDDDD")
    border = Border(left=thin, right=thin, top=thin, bottom=thin)
    wrap = Alignment(vertical="top", wrap_text=True)

    ws.merge_cells(f"A1:{last_col}1")
    t = ws["A1"]
    t.value = f"Acme Services — Possible Billable Email Activity  ({label})"
    t.font = Font(bold=True, size=14, color="1E3A5F")
    t.alignment = Alignment(horizontal="left", vertical="center")
    ws.row_dimensions[1].height = 26

    for col, h in enumerate(headers, 1):
        c = ws.cell(row=2, column=col, value=h)
        c.fill = header_fill
        c.font = header_font
        c.alignment = Alignment(vertical="center", wrap_text=True)
        c.border = border

    r = 3
    for task in tasks:
        ws.cell(row=r, column=1, value=cell_text(task.get("date", "")))
        ws.cell(row=r, column=2, value=cell_text(task.get("subject", "")))
        ws.cell(row=r, column=3, value=cell_text(task.get("people", "")))
        ws.cell(row=r, column=4, value=canonical_client(task.get("client", "")))
        ws.cell(row=r, column=5, value=cell_text(task.get("summary", "")))
        link = cell_text(task.get("link", ""))
        link_cell = ws.cell(row=r, column=6)
        if link:
            link_cell.value = "Open in Gmail"
            link_cell.hyperlink = link
            link_cell.font = link_font
        ws.cell(row=r, column=7, value="")   # Est. Billable Time (you fill in)
        ws.cell(row=r, column=8, value="")   # Notes (you fill in)
        for col in range(1, ncols + 1):
            cell = ws.cell(row=r, column=col)
            cell.border = border
            if col != 6:
                cell.alignment = wrap
        r += 1

    if not tasks:
        ws.cell(row=3, column=1,
                value="No billable-relevant email activity found for this period.")

    for i, w in enumerate([12, 38, 24, 22, 60, 14, 16, 26], 1):
        ws.column_dimensions[get_column_letter(i)].width = w
    ws.freeze_panes = "A3"

    os.makedirs(OUTPUT_DIR, exist_ok=True)
    fname = f"billable_email_activity_{file_tag}.xlsx"
    path = os.path.join(OUTPUT_DIR, fname)
    wb.save(path)
    log(f"spreadsheet written: {path}")

    if os.path.isdir(NAS_COPY_DIR):
        try:
            import shutil
            shutil.copy(path, os.path.join(NAS_COPY_DIR, fname))
            log(f"copied to NAS: {NAS_COPY_DIR}")
        except Exception as e:
            log(f"NAS copy failed (non-fatal): {e}")
    return path


# ── NOTIFICATION EMAIL ────────────────────────────────────────────────────────
def notify(person: dict, path: str, count: int, label: str):
    first = person["name"].split()[0]
    subject = f"Billable Email Activity — {label} ({count} items)"
    body = (
        f"Morning {first},\n\n"
        f"I went through the inbox for {label} and pulled {count} item(s) that "
        f"look like client or tenant work handled by email.\n\n"
        f"The report is attached as a spreadsheet — open it and fill in the two "
        f"blank columns (Est. Billable Time and Notes). The other columns (Date, "
        f"People, Client/Property, Summary) are filled in for you.\n\n"
        f"These are possible items only — your call on what's actually billable.\n\n"
        f"— David  |  Acme Services Automation"
    )
    env = os.environ.copy()
    env["GOG_KEYRING_PASSWORD"] = GOG_KEYRING
    cmd = ["gog", "gmail", "send",
           "--to", ",".join(person["notify"]),
           "--subject", subject, "--body", body,
           "--account", SENDER]
    if path and os.path.isfile(path):
        cmd += ["--attach", path]
    else:
        body += f"\n(Spreadsheet at: {path})"
    result = subprocess.run(cmd, env=env, capture_output=True, text=True,
                            encoding="utf-8", errors="replace")
    if result.returncode == 0:
        log(f"report emailed to {', '.join(person['notify'])}")
    else:
        log(f"notification send failed: {result.stderr.strip()}")


# ── DATE RANGE / ARGS ─────────────────────────────────────────────────────────
def build_query_and_label(args):
    base = " -in:spam -in:trash"

    if args.month:
        y, m = map(int, args.month.split("-"))
        start = date(y, m, 1)
        end = date(y + 1, 1, 1) if m == 12 else date(y, m + 1, 1)  # exclusive
        q = f"after:{start.strftime('%Y/%m/%d')} before:{end.strftime('%Y/%m/%d')}"
        return q + base, start.strftime("%B %Y"), f"{y}-{m:02d}"

    if args.after or args.before:
        if not (args.after and args.before):
            sys.exit("Provide BOTH --after and --before (YYYY-MM-DD).")
        start = datetime.strptime(args.after, "%Y-%m-%d").date()
        end = datetime.strptime(args.before, "%Y-%m-%d").date()  # exclusive
        q = f"after:{start.strftime('%Y/%m/%d')} before:{end.strftime('%Y/%m/%d')}"
        label = f"{start.strftime('%b %d')} – {(end - timedelta(days=1)).strftime('%b %d, %Y')}"
        tag = f"{start.isoformat()}_to_{(end - timedelta(days=1)).isoformat()}"
        return q + base, label, tag

    days = args.days or DEFAULT_DAYS
    today = date.today()
    q = f"newer_than:{days}d"
    label = f"{(today - timedelta(days=days)).strftime('%b %d')} – {today.strftime('%b %d, %Y')}"
    return q + base, label, f"{today.isoformat()}_{days}d"


def run_for_person(key: str, person: dict, args):
    """Run the whole pipeline for one person."""
    query, label, base_tag = build_query_and_label(args)
    tag = f"{key}_{base_tag}"   # per-person tag keeps caches/outputs separate

    log(f"=== {person['name']} ({person['email']}) — {label} ===")
    log(f"query: {query}")

    blocks, metas = gather_email_blocks(query, tag, person["email"],
                                        refresh=args.refresh)
    tasks = extract_tasks(blocks, tag, person["name"], person["role"])
    log(f"Gemini surfaced {len(tasks)} possible billable items for {person['name']}")

    # Map each item back to its thread to fill in Subject and a clickable link
    for task in tasks:
        meta = None
        raw_n = cell_text(task.get("thread", ""))
        digits = re.sub(r"[^0-9]", "", raw_n)
        if digits:
            idx = int(digits) - 1
            if 0 <= idx < len(metas):
                meta = metas[idx]
        if meta:
            task["subject"] = meta.get("subject", "")
            tid = meta.get("id", "")
            if tid:
                task["link"] = (f"https://mail.google.com/mail/u/0/"
                                f"?authuser={person['email']}#all/{tid}")

    os.makedirs(OUTPUT_DIR, exist_ok=True)
    json_path = os.path.join(OUTPUT_DIR, f"billable_email_activity_{tag}.json")
    try:
        with open(json_path, "w", encoding="utf-8") as f:
            json.dump(tasks, f, indent=2, ensure_ascii=False)
        log(f"raw items saved: {json_path}")
    except Exception as e:
        log(f"could not save raw items (non-fatal): {e}")

    person_label = f"{person['name'].split()[0]} — {label}"
    path = build_spreadsheet(tasks, person_label, tag)
    notify(person, path, len(tasks), label)


# ── MAIN ──────────────────────────────────────────────────────────────────────
def main():
    ap = argparse.ArgumentParser(description="Acme Services billable email finder")
    ap.add_argument("--person", default="tony",
                    help="Who to run for: " + ", ".join(PEOPLE) + ", or 'all' "
                         "(default: tony)")
    ap.add_argument("--month", help="Whole calendar month, e.g. 2026-05")
    ap.add_argument("--after", help="Range start, inclusive, YYYY-MM-DD")
    ap.add_argument("--before", help="Range end, exclusive, YYYY-MM-DD")
    ap.add_argument("--days", type=int, help="Rolling window in days (default 7)")
    ap.add_argument("--refresh", action="store_true",
                    help="Ignore cached threads and re-fetch from Gmail")
    args = ap.parse_args()

    who = args.person.lower()
    if who == "all":
        selected = list(PEOPLE.items())
    elif who in PEOPLE:
        selected = [(who, PEOPLE[who])]
    else:
        sys.exit(f"Unknown --person '{args.person}'. Choose: {', '.join(PEOPLE)}, or all.")

    for key, person in selected:
        try:
            run_for_person(key, person, args)
        except Exception as e:
            log(f"ERROR running for {person['name']}: {e}")
            log("  continuing with the next person")

    log("done")


if __name__ == "__main__":
    main()
