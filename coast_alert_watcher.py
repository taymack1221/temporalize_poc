#!/usr/bin/env python3
# ─────────────────────────────────────────────────────────────────────────────
# coast_alert_watcher.py
#
# Rings a bell every time a NEW Coast work request comes in.
#
# How it works:
#   - Polls bot@example.com's Gmail every POLL_SECONDS via `gog`
#   - David's inbox only ever gets NEW-CARD notifications from Coast (per Owner's
#     Coast automation — David isn't assigned to tickets, so he never receives
#     update/comment emails). So the sender alone is a reliable trigger:
#         From: noreply@coastapp.com
#   - Coast sends TWO different email templates for a new request; the parser
#     handles both:
#       A) "...submitted: <title> (Work Order ID: 27 )"  + a coastapp.com link
#       B) "...submitted: Ticket #: 3,333 ."  / "Issue: <desc>" / "Notes: ..."  (no link)
#   - Fires two alerts:  (1) office chime on the Pi   (2) phone push
#   - De-dupes via a small state file so nothing ever rings twice
#
# Non-destructive: does NOT mark emails read or move them. Dedupe is by message
# ID in STATE_FILE, bounded by a `newer_than:1d` search window.
#
# Modes:
#   python3 coast_alert_watcher.py            # run the poll loop (what systemd runs)
#   python3 coast_alert_watcher.py --once     # single poll pass, then exit
#   python3 coast_alert_watcher.py --probe    # dump raw gog search JSON
#   python3 coast_alert_watcher.py --test     # fire both alerts with dummy data
# ─────────────────────────────────────────────────────────────────────────────

import os
import re
import sys
import json
import time
import base64
import subprocess
from datetime import datetime

# ── CONFIG ───────────────────────────────────────────────────────────────────

# --- Gmail / gog -------------------------------------------------------------
DAVID_ACCOUNT        = "bot@example.com"
GOG_KEYRING_PASSWORD = "YOUR_GOG_KEYRING_PASSWORD"

# --- Coast new-request signal (verified from real emails, 07/29/2026) --------
COAST_FROM = "noreply@coastapp.com"

# --- Dedupe ------------------------------------------------------------------
STATE_FILE = "/home/user/coast_alert_seen.json"
STATE_KEEP = 500  # how many recent IDs to remember

# --- Alert counter (shown on the office board / 4K TV) -----------------------
# Written into the port-8080 web root so office_board.html can fetch it live.
# Carries the date so it auto-resets at the day rollover (see bump_counter).
COUNT_FILE = "/home/user/weather/coast_alert_count.json"

# --- Poll cadence ------------------------------------------------------------
POLL_SECONDS = 45

# --- Office chime: Raspberry Pi that drives the 4K TV ------------------------
#   NOTE: confirm the Pi's real IP + user before going live.
ENABLE_OFFICE_CHIME = True
PI_HOST  = "192.168.1.50"          # Raspberry Pi (drives the 4K TV)
PI_USER  = "piuser"
BELL_WAV = "/home/piuser/coast_bell.wav"   # a .wav sitting on the Pi

# --- Phone alert -------------------------------------------------------------
#   "ntfy"     -> free push app, distinct bell sound, instant   (recommended)
#   "email"    -> emails you via gog; guaranteed, but normal mail tone
#   "whatsapp" -> via David  (needs the send command confirmed — stubbed below)
PHONE_METHOD = "ntfy"
NTFY_TOPIC   = "your-ntfy-topic"   # private topic; subscribe to this in the ntfy app
NTFY_PRIORITY = "high"   # min | low | default | high | urgent  (urgent = most insistent on iOS)
TONY_EMAIL   = "owner@example.com"

# ─────────────────────────────────────────────────────────────────────────────


def _env():
    e = os.environ.copy()
    e["GOG_KEYRING_PASSWORD"] = GOG_KEYRING_PASSWORD
    e["GOG_KEYRING_BACKEND"]  = "file"   # required headless (systemd/cron), no desktop keyring
    return e


def log(msg):
    print(f"[coast-alert {datetime.now():%H:%M:%S}] {msg}", flush=True)


# ── STATE (dedupe) ────────────────────────────────────────────────────────────

def load_seen():
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except Exception:
        return []


def save_seen(seen):
    try:
        with open(STATE_FILE, "w") as f:
            json.dump(seen[-STATE_KEEP:], f)
    except Exception as e:
        log(f"WARN could not write state file: {e}")


def bump_counter():
    """Increment today's Coast-alert count; auto-resets at the day rollover."""
    today = datetime.now().strftime("%Y-%m-%d")
    try:
        with open(COUNT_FILE) as f:
            data = json.load(f)
    except Exception:
        data = {}
    if data.get("date") != today:          # new day (or first run) -> start fresh
        data = {"date": today, "count": 0}
    data["count"] = int(data.get("count", 0)) + 1
    try:
        with open(COUNT_FILE, "w") as f:
            json.dump(data, f)
        log(f"  alert count -> {data['count']} today")
    except Exception as e:
        log(f"  WARN could not write count file: {e}")


# ── GMAIL via gog ─────────────────────────────────────────────────────────────

def gog_search():
    """Return raw JSON stdout of a gog Gmail search for new Coast requests."""
    # gog v0.11.0: query is POSITIONAL (not a flag); --json for clean parsing.
    query = f'from:{COAST_FROM} newer_than:1d'
    cmd = ["gog", "gmail", "search", query,
           "--account", DAVID_ACCOUNT, "--json", "--max", "20"]
    r = subprocess.run(cmd, env=_env(), capture_output=True, text=True)
    if r.returncode != 0:
        log(f"gog search error: {r.stderr.strip()}")
    return r.stdout


def extract_thread_ids(raw):
    """Parse gog --json search output; return thread IDs, newest first."""
    try:
        data = json.loads(raw)
    except Exception as e:
        log(f"could not parse search JSON: {e}")
        return []
    return [t["id"] for t in data.get("threads", []) if t.get("id")]


def _decode_b64url(data):
    data = data.replace('-', '+').replace('_', '/')
    data += '=' * (-len(data) % 4)
    return base64.b64decode(data).decode('utf-8', errors='replace')


def _collect_bodies(payload, out):
    b = payload.get("body", {}).get("data")
    if b:
        out.append((payload.get("mimeType", ""), b))
    for p in payload.get("parts", []) or []:
        _collect_bodies(p, out)


def _body_from_payload(payload):
    """Decode a single message payload to text (prefers text/plain, else strips HTML)."""
    bodies = []
    _collect_bodies(payload, bodies)
    for mime, b in bodies:
        if mime.startswith("text/plain"):
            return _decode_b64url(b)
    for mime, b in bodies:
        if mime.startswith("text/html"):
            return re.sub(r'<[^>]+>', ' ', _decode_b64url(b))
    return _decode_b64url(bodies[0][1]) if bodies else ""


def fetch_messages(thread_id):
    """gog thread get --json -> list of (message_id, body_text) for EVERY message.
    Gmail groups same-subject Coast emails into one thread, so we must look at
    each message individually rather than the thread as a whole."""
    cmd = ["gog", "gmail", "thread", "get", thread_id,
           "--json", "--account", DAVID_ACCOUNT]
    r = subprocess.run(cmd, env=_env(), capture_output=True, text=True)
    if r.returncode != 0:
        log(f"gog thread get error ({thread_id}): {r.stderr.strip()}")
        return []
    try:
        data = json.loads(r.stdout)
        out = []
        for m in data.get("thread", {}).get("messages", []):
            mid = m.get("id")
            if mid:
                out.append((mid, _body_from_payload(m.get("payload", {}))))
        return out
    except Exception as e:
        log(f"could not parse thread JSON ({thread_id}): {e}")
        return []


# ── PARSE the Coast email body (handles both templates) ───────────────────────

def parse_ticket(body):
    ticket_id = None
    m = (re.search(r'Work Order ID:\s*([\d,]+)', body, re.I) or
         re.search(r'Ticket\s*#:\s*([\d,]+)', body, re.I))
    if m:
        ticket_id = m.group(1).replace(',', '').strip()

    # Best description: prefer the "Issue:" line (Template B); else the text
    # between "submitted:" and "(Work Order ID:" (Template A).
    title = None
    m = re.search(r'Issue:[ \t]*([^\r\n]+)', body, re.I)
    if m and m.group(1).strip():
        title = m.group(1).strip()
    else:
        m = re.search(r'submitted:\s*(.+?)\s*\(Work Order ID:', body, re.I | re.S)
        if m:
            title = m.group(1).strip()

    notes = None
    m = re.search(r'Notes:[ \t]*([^\r\n]+)', body, re.I)
    if m and m.group(1).strip():
        notes = m.group(1).strip()

    link = None
    m = re.search(r'https://app\.coastapp\.com/\S+', body)
    if m:
        link = m.group(0).rstrip(').,')

    return {"title": title or "(no description)",
            "ticket_id": ticket_id or "?",
            "notes": notes,
            "link": link or ""}


# ── ALERTS ────────────────────────────────────────────────────────────────────

def chime_office(ticket):
    if not ENABLE_OFFICE_CHIME:
        return
    cmd = ["ssh", "-o", "ConnectTimeout=5", f"{PI_USER}@{PI_HOST}",
           f"aplay -q {BELL_WAV}"]
    r = subprocess.run(cmd, capture_output=True, text=True)
    log("  office chime: OK" if r.returncode == 0 else f"  office chime FAILED: {r.stderr.strip()}")
    # TODO(optional): flash an on-screen banner on the 4K board here.


def alert_phone(ticket):
    msg = f"{ticket['title']}  (Ticket #{ticket['ticket_id']})"
    if ticket['notes']:
        msg += f"\n{ticket['notes']}"
    if PHONE_METHOD == "ntfy":
        _phone_ntfy(msg, ticket)
    elif PHONE_METHOD == "email":
        _phone_email(msg, ticket)
    elif PHONE_METHOD == "whatsapp":
        _phone_whatsapp(msg, ticket)
    else:
        log(f"  unknown PHONE_METHOD '{PHONE_METHOD}' — skipping phone alert")


def _phone_ntfy(msg, ticket):
    headers = ["-H", "Title: \U0001F514 New Coast Request",
               "-H", "Tags: bell,wrench",
               "-H", f"Priority: {NTFY_PRIORITY}"]
    if ticket['link']:
        headers += ["-H", f"Click: {ticket['link']}"]
    cmd = ["curl", "-s", *headers, "-d", msg, f"https://ntfy.sh/{NTFY_TOPIC}"]
    r = subprocess.run(cmd, capture_output=True, text=True)
    log("  phone (ntfy): OK" if r.returncode == 0 else f"  phone (ntfy) FAILED: {r.stderr.strip()}")


def _phone_email(msg, ticket):
    subject = f"\U0001F514 New Coast Request — {ticket['title']} (Ticket #{ticket['ticket_id']})"
    body = msg + (f"\n\nOpen ticket: {ticket['link']}" if ticket['link'] else "")
    cmd = ["gog", "gmail", "send", "--to", TONY_EMAIL,
           "--subject", subject, "--body", body, "--account", DAVID_ACCOUNT]
    r = subprocess.run(cmd, env=_env(), capture_output=True, text=True)
    log("  phone (email): OK" if r.returncode == 0 else f"  phone (email) FAILED: {r.stderr.strip()}")


def _phone_whatsapp(msg, ticket):
    # TODO: wire to David's WhatsApp send path once confirmed. Stubbed so we
    # don't guess the mechanism.
    log("  phone (whatsapp): NOT WIRED — confirm David's send command first")


def fire_alerts(ticket):
    log(f"NEW REQUEST -> {ticket['title']} (Ticket #{ticket['ticket_id']})")
    chime_office(ticket)
    alert_phone(ticket)


# ── MAIN POLL ─────────────────────────────────────────────────────────────────

def poll_once():
    thread_ids = extract_thread_ids(gog_search())
    if not thread_ids:
        return
    seen = load_seen()          # now a list of MESSAGE ids
    fired = False
    for tid in thread_ids:
        for mid, body in fetch_messages(tid):
            if mid in seen:
                continue
            fire_alerts(parse_ticket(body))
            bump_counter()
            seen.append(mid)
            fired = True
    if fired:
        save_seen(seen)


def run_loop():
    log(f"watcher up — polling every {POLL_SECONDS}s for new Coast requests")
    while True:
        try:
            poll_once()
        except Exception as e:
            log(f"poll error: {e}")
        time.sleep(POLL_SECONDS)


def baseline_seen():
    """Mark every Coast message currently in the inbox as already-seen, WITHOUT
    firing — a clean starting line so only genuinely new cards ring afterward."""
    seen = load_seen()
    added = 0
    for tid in extract_thread_ids(gog_search()):
        for mid, _ in fetch_messages(tid):
            if mid not in seen:
                seen.append(mid)
                added += 1
    save_seen(seen)
    log(f"baseline: marked {added} existing message(s) as seen ({len(seen)} total)")


# ── ENTRYPOINTS ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    arg = sys.argv[1] if len(sys.argv) > 1 else ""
    if arg == "--probe":
        print(gog_search())
    elif arg == "--baseline":
        baseline_seen()
    elif arg == "--test":
        fire_alerts({"title": "TEST — First Floor Bathroom leak",
                     "ticket_id": "0000", "notes": None,
                     "link": "https://app.coastapp.com/test"})
    elif arg == "--once":
        poll_once()
    else:
        run_loop()
