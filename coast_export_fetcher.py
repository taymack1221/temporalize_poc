#!/usr/bin/env python3
# ─────────────────────────────────────────────────────────────────────────────
# coast_export_fetcher.py
#
# Automates the Coast CSV export DOWNLOAD (you still click "Export" in Coast).
# Watches bot@example.com's inbox for "Your Coast export is ready", pulls the
# S3 link from the body, downloads the CSV, writes it to both the board's file
# and the digest's file, then regenerates the board stats.
#
# Zero tokens — inbox polling + an HTTP download, no AI.
#
# Note: the S3 link expires 1 hour after the email (X-Amz-Expires=3600), so the
# watcher grabs it promptly. If a link has already expired, the download is
# skipped with a clear log line — just re-export.
#
# Modes:
#   (default)   poll loop (what systemd runs)
#   --once      single pass, then exit  (handy right after you click Export)
#   --probe     dump the raw gog search JSON
#   --baseline  mark existing export emails as seen WITHOUT downloading
# ─────────────────────────────────────────────────────────────────────────────

import os, re, sys, json, time, base64, subprocess, urllib.request
import html as htmllib
from datetime import datetime

# ── CONFIG ───────────────────────────────────────────────────────────────────
DAVID_ACCOUNT        = "bot@example.com"
GOG_KEYRING_PASSWORD = "YOUR_GOG_KEYRING_PASSWORD"

COAST_FROM    = "noreply@coastapp.com"
COAST_SUBJECT = "Your Coast export is ready"   # distinct from ticket-alert emails

# Where the CSV gets written (board reads coast_new; digest reads coast_export)
CSV_PATHS    = ["/mnt/c/CoastExport/coast_new.csv",
                "/mnt/c/CoastExport/coast_export.csv"]
STATS_SCRIPT = "/home/user/coast_stats.py"    # run after a successful download

STATE_FILE = "/home/user/coast_export_seen.json"
STATE_KEEP = 200
POLL_SECONDS = 60
# ─────────────────────────────────────────────────────────────────────────────


def _env():
    e = os.environ.copy()
    e["GOG_KEYRING_PASSWORD"] = GOG_KEYRING_PASSWORD
    e["GOG_KEYRING_BACKEND"]  = "file"
    return e

def log(m):
    print(f"[coast-export {datetime.now():%H:%M:%S}] {m}", flush=True)


# ── state / dedupe (per message id) ───────────────────────────────────────────
def load_seen():
    try:
        with open(STATE_FILE) as f: return json.load(f)
    except Exception: return []

def save_seen(seen):
    try:
        with open(STATE_FILE, "w") as f: json.dump(seen[-STATE_KEEP:], f)
    except Exception as e: log(f"WARN state write failed: {e}")


# ── gmail via gog (same proven machinery as the alert watcher) ────────────────
def gog_search():
    query = f'from:{COAST_FROM} subject:"{COAST_SUBJECT}" newer_than:1d'
    cmd = ["gog", "gmail", "search", query, "--account", DAVID_ACCOUNT, "--json", "--max", "20"]
    r = subprocess.run(cmd, env=_env(), capture_output=True, text=True)
    if r.returncode != 0: log(f"gog search error: {r.stderr.strip()}")
    return r.stdout

def thread_ids(raw):
    try: data = json.loads(raw)
    except Exception as e:
        log(f"search JSON parse error: {e}"); return []
    return [t["id"] for t in data.get("threads", []) if t.get("id")]

def _decode_b64url(data):
    data = data.replace('-', '+').replace('_', '/'); data += '=' * (-len(data) % 4)
    return base64.b64decode(data).decode('utf-8', errors='replace')

def _collect(payload, out):
    b = payload.get("body", {}).get("data")
    if b: out.append((payload.get("mimeType", ""), b))
    for p in payload.get("parts", []) or []: _collect(p, out)

def fetch_messages(tid):
    cmd = ["gog", "gmail", "thread", "get", tid, "--json", "--account", DAVID_ACCOUNT]
    r = subprocess.run(cmd, env=_env(), capture_output=True, text=True)
    if r.returncode != 0:
        log(f"thread get error ({tid}): {r.stderr.strip()}"); return []
    try:
        data = json.loads(r.stdout); out = []
        for m in data.get("thread", {}).get("messages", []):
            mid = m.get("id")
            if not mid: continue
            bodies = []; _collect(m.get("payload", {}), bodies)
            text = ""
            for mime, b in bodies:                         # prefer plain, else HTML
                if mime.startswith("text/plain"): text = _decode_b64url(b); break
            if not text:
                for mime, b in bodies:
                    if mime.startswith("text/html"): text = _decode_b64url(b); break
            if not text and bodies: text = _decode_b64url(bodies[0][1])
            out.append((mid, text))
        return out
    except Exception as e:
        log(f"thread JSON parse error ({tid}): {e}"); return []


# ── the export-specific bits ──────────────────────────────────────────────────
def extract_s3_url(body):
    body = body.replace('=\r\n', '').replace('=\n', '')   # undo QP soft-wraps only
    body = htmllib.unescape(body)                          # &amp; -> &
    m = re.search(r"https://coast-exports-production\.s3[^\s\"'<>\\)]+", body)
    return m.group(0) if m else None

def download_csv(url):
    """Download the export; return bytes, or None if expired/not-a-CSV."""
    req = urllib.request.Request(url, headers={"User-Agent": "acme-coast-fetcher"})
    with urllib.request.urlopen(req, timeout=90) as r:
        data = r.read()
    head = data[:64].lstrip()
    if head[:5] in (b"<?xml", b"<Erro") or b"AccessDenied" in data[:300] or b"Expired" in data[:300]:
        log("  download: link expired / access denied (re-export to refresh)")
        return None
    return data

def write_and_refresh(data):
    for p in CSV_PATHS:
        try:
            os.makedirs(os.path.dirname(p), exist_ok=True)
            with open(p, "wb") as f: f.write(data)
            log(f"  wrote {len(data)} bytes -> {p}")
        except Exception as e:
            log(f"  WARN write failed {p}: {e}")
    # regenerate the board stats from the fresh CSV
    r = subprocess.run(["python3", STATS_SCRIPT], env=_env(), capture_output=True, text=True)
    log("  coast_stats.py: refreshed" if r.returncode == 0 else f"  coast_stats.py FAILED: {r.stderr.strip()[:200]}")


def handle_message(mid, body):
    url = extract_s3_url(body)
    if not url:
        log(f"  no S3 link found in export email {mid} — skipping"); return
    log(f"export email {mid}: downloading CSV…")
    try:
        data = download_csv(url)
    except Exception as e:
        log(f"  download error: {e}"); return
    if data:
        write_and_refresh(data)


# ── poll ──────────────────────────────────────────────────────────────────────
def poll_once():
    ids = thread_ids(gog_search())
    if not ids: return
    seen = load_seen(); changed = False
    for tid in ids:
        for mid, body in fetch_messages(tid):
            if mid in seen: continue
            handle_message(mid, body)
            seen.append(mid); changed = True
    if changed: save_seen(seen)

def baseline():
    seen = load_seen(); added = 0
    for tid in thread_ids(gog_search()):
        for mid, _ in fetch_messages(tid):
            if mid not in seen: seen.append(mid); added += 1
    save_seen(seen)
    log(f"baseline: marked {added} existing export email(s) as seen ({len(seen)} total)")

def run_loop():
    log(f"export fetcher up — polling every {POLL_SECONDS}s")
    while True:
        try: poll_once()
        except Exception as e: log(f"poll error: {e}")
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    arg = sys.argv[1] if len(sys.argv) > 1 else ""
    if   arg == "--probe":    print(gog_search())
    elif arg == "--baseline": baseline()
    elif arg == "--once":     poll_once()
    else:                     run_loop()
