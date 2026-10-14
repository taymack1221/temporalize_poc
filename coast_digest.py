#!/usr/bin/env python3
"""
Acme Services Coast Work Order Digest
Reads the Coast CSV export, generates a daily morning digest email to Owner
highlighting stale, high priority, and newly opened tickets.

Deploy:  /home/user/coast_digest.py
CSV:     /mnt/c/CoastExport/coast_export.csv  (update path as needed)
Cron:    0 6 * * 1-5  python3 /home/user/coast_digest.py >> /home/user/coast_digest.log 2>&1

NOTE: updated for the NEW Coast platform CSV layout (Aug 2026).
  Work Order Status (was Status) | Work Order ID (was Card ID Number)
  Title (was Subject)            | Location (was Building)
  Priority is currently unused on the new platform (all "None"), so the
  High Priority section will stay empty until priorities are set. Same for
  On Hold — that status doesn't exist in the new platform's set.
"""

import csv
import os
import subprocess
from datetime import datetime, timezone, timedelta
from collections import defaultdict

# ── CONFIG ────────────────────────────────────────────────────────────────────
CSV_PATH  = "/mnt/c/CoastExport/coast_export.csv"
SENDER    = "bot@example.com"
RECIPIENT = "owner@example.com"          # primary (To)
CC_RECIPIENTS = ["manager1@example.com", "manager2@example.com"]   # also receive the digest (Cc)

# Thresholds (days)
CRITICAL_AGE = 30    # red — needs a decision
WARNING_AGE  = 14    # yellow — getting stale
WATCH_AGE    = 7     # blue — keep an eye on

# Employee email map for tech notifications
TECH_EMAILS = {
    "Employee 02":      "manager1@example.com",
    "Employee 01":   "owner@example.com",
    "Employee 03":       "manager2@example.com",
    "Employee 04":    "tech01@example.com",
    "Employee 05":       "tech02@example.com",
    "Employee 06": "tech03@example.com",
    "Employee 07":    "manager3@example.com",
    "Employee 08":      "tech04@example.com",
    "Employee 09":     "tech05@example.com",
    "Employee 10":        "tech06@example.com",
    "Employee 11":   "tech07@example.com",
    "Employee 12":     "tech08@example.com",
    "Employee 13":    "tech09@example.com",
    "Employee 14":    "tech10@example.com",
    "Employee 15": "tech11@example.com",
    "Employee 16":    "tech12@example.com",
}

LOGO_SRC = ""

# Active statuses to monitor (NEW platform values, lowercased)
ACTIVE_STATUSES = {"open", "in progress", "scheduled", "pending review"}

# ── DATA LOADING ──────────────────────────────────────────────────────────────
def load_tickets(path: str) -> list[dict]:
    tickets = []
    try:
        with open(path, newline="", encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            for row in reader:
                status = row.get("Work Order Status", "").strip().lower()
                if status not in ACTIVE_STATUSES:
                    continue
                created = row.get("Created On", "").strip()
                updated = row.get("Updated On", "").strip()
                try:
                    created_dt = datetime.fromisoformat(created)
                    if created_dt.tzinfo is None:
                        created_dt = created_dt.replace(tzinfo=timezone.utc)
                except:
                    created_dt = None
                try:
                    updated_dt = datetime.fromisoformat(updated)
                    if updated_dt.tzinfo is None:
                        updated_dt = updated_dt.replace(tzinfo=timezone.utc)
                except:
                    updated_dt = created_dt

                now = datetime.now(timezone.utc)
                age_days    = (now - created_dt).days if created_dt else 0
                stale_days  = (now - updated_dt).days if updated_dt else age_days

                tickets.append({
                    "id":          row.get("Work Order ID", "").strip(),
                    "status":      row.get("Work Order Status", "").strip(),
                    "priority":    row.get("Priority", "").strip(),
                    "subject":     (row.get("Title", "").strip() or row.get("Notes", "").strip()),
                    "building":    (row.get("Location", "").strip() or row.get("Zone", "").strip()).replace("\n", ", "),
                    "tenant":      row.get("Tenant", "").strip(),
                    "assignee":    row.get("Assignee", "").strip().replace("\n", ", "),
                    "notes":       row.get("Notes", "").strip(),
                    "created_dt":  created_dt,
                    "updated_dt":  updated_dt,
                    "age_days":    age_days,
                    "stale_days":  stale_days,
                    "permalink":   row.get("Permalink", "").strip(),
                })
    except FileNotFoundError:
        print(f"ERROR: CSV not found at {path}")
    return tickets


def categorize(tickets: list[dict]) -> dict:
    now = datetime.now(timezone.utc)
    yesterday = now - timedelta(days=1)

    cats = {
        "critical":   [],   # >30 days old, still open
        "warning":    [],   # 14-30 days
        "watch":      [],   # 7-14 days
        "high_pri":   [],   # high priority regardless of age
        "new_today":  [],   # created in last 24h
        "on_hold":    [],   # explicitly on hold
    }

    for t in tickets:
        if t["created_dt"] and t["created_dt"] >= yesterday:
            cats["new_today"].append(t)
        if t["priority"].lower() in ("high", "urgent") and t["age_days"] > 1:
            cats["high_pri"].append(t)
        if t["status"].lower() == "on hold":
            cats["on_hold"].append(t)
        if t["age_days"] >= CRITICAL_AGE:
            cats["critical"].append(t)
        elif t["age_days"] >= WARNING_AGE:
            cats["warning"].append(t)
        elif t["age_days"] >= WATCH_AGE:
            cats["watch"].append(t)

    # Sort each by age descending
    for k in cats:
        cats[k].sort(key=lambda x: -x["age_days"])

    return cats


# ── HTML BUILDER ──────────────────────────────────────────────────────────────
def ticket_row(t: dict, bg: str = "#fff") -> str:
    age_color = "#c0392b" if t["age_days"] >= CRITICAL_AGE else \
                "#e67e22" if t["age_days"] >= WARNING_AGE else \
                "#2980b9" if t["age_days"] >= WATCH_AGE else "#27ae60"

    pri = t["priority"]
    pri_badge = ""
    if pri.lower() in ("high", "urgent"):
        pri_badge = f'<span style="background:#c0392b;color:#fff;font-size:10px;padding:2px 6px;border-radius:10px;margin-left:6px;">{pri}</span>'
    elif pri.lower() == "medium":
        pri_badge = f'<span style="background:#e67e22;color:#fff;font-size:10px;padding:2px 6px;border-radius:10px;margin-left:6px;">{pri}</span>'

    link = f'<a href="{t["permalink"]}" style="color:#1a56db;text-decoration:none;">#{t["id"]}</a>' if t["permalink"] else f'#{t["id"]}'

    assignee = t["assignee"] if t["assignee"] else "<span style='color:#999;font-style:italic;'>Unassigned</span>"

    return f"""
    <tr style="background:{bg};border-bottom:1px solid #f0f0f0;">
      <td style="padding:10px 12px;font-size:12px;color:#666;">{link}</td>
      <td style="padding:10px 12px;">
        <div style="font-size:13px;font-weight:500;color:#222;">{t['subject'][:70]}{pri_badge}</div>
        <div style="font-size:11px;color:#888;margin-top:2px;">{t['building'][:50]}</div>
        {f'<div style="font-size:11px;color:#aaa;">{t["tenant"][:40]}</div>' if t['tenant'] else ''}
      </td>
      <td style="padding:10px 12px;font-size:12px;color:#555;">{t['status']}</td>
      <td style="padding:10px 12px;font-size:12px;color:#555;">{assignee[:30]}</td>
      <td style="padding:10px 12px;font-size:13px;font-weight:600;color:{age_color};text-align:center;">{t['age_days']}d</td>
    </tr>"""


def section_html(title: str, color: str, icon: str, tickets: list[dict], note: str = "") -> str:
    if not tickets:
        return ""
    rows = "".join(ticket_row(t, "#fff" if i % 2 == 0 else "#fafafa") for i, t in enumerate(tickets))
    note_html = f'<div style="font-size:11px;color:#888;padding:6px 16px 10px;">{note}</div>' if note else ""
    return f"""
    <div style="margin-bottom:24px;border-radius:10px;overflow:hidden;box-shadow:0 2px 8px rgba(0,0,0,0.07);">
      <div style="background:{color};padding:12px 16px;display:flex;align-items:center;gap:10px;">
        <span style="font-size:18px;">{icon}</span>
        <span style="color:#fff;font-weight:600;font-size:14px;">{title}</span>
        <span style="margin-left:auto;background:rgba(255,255,255,0.2);color:#fff;font-size:12px;padding:2px 10px;border-radius:12px;">{len(tickets)} ticket{'s' if len(tickets) != 1 else ''}</span>
      </div>
      {note_html}
      <table style="width:100%;border-collapse:collapse;font-family:Arial,sans-serif;">
        <tr style="background:#f5f5f5;">
          <th style="padding:8px 12px;text-align:left;font-size:11px;color:#666;font-weight:600;border-bottom:2px solid #e0e0e0;">TICKET</th>
          <th style="padding:8px 12px;text-align:left;font-size:11px;color:#666;font-weight:600;border-bottom:2px solid #e0e0e0;">SUBJECT / BUILDING</th>
          <th style="padding:8px 12px;text-align:left;font-size:11px;color:#666;font-weight:600;border-bottom:2px solid #e0e0e0;">STATUS</th>
          <th style="padding:8px 12px;text-align:left;font-size:11px;color:#666;font-weight:600;border-bottom:2px solid #e0e0e0;">ASSIGNEE</th>
          <th style="padding:8px 12px;text-align:center;font-size:11px;color:#666;font-weight:600;border-bottom:2px solid #e0e0e0;">AGE</th>
        </tr>
        {rows}
      </table>
    </div>"""


def build_html(tickets: list[dict], cats: dict, today: datetime) -> str:
    total_active = len(tickets)
    today_str = today.strftime("%A, %B %d, %Y")

    critical_sec = section_html(
        "🔴 Needs a Decision — Over 30 Days", "#c0392b", "🔴",
        cats["critical"],
        "These tickets have been open more than 30 days. Each needs to either be actioned, escalated, or closed."
    )
    highpri_sec = section_html(
        "⚡ High Priority", "#e74c3c", "⚡",
        [t for t in cats["high_pri"] if t not in cats["critical"]],
        "High priority tickets that need prompt attention."
    )
    onhold_sec = section_html(
        "⏸ On Hold", "#8e44ad", "⏸",
        [t for t in cats["on_hold"] if t not in cats["critical"]],
        "Tickets explicitly placed on hold. Review regularly to ensure they haven't been forgotten."
    )
    warning_sec = section_html(
        "🟡 Getting Stale — 14 to 30 Days", "#e67e22", "🟡",
        [t for t in cats["warning"] if t["status"].lower() != "on hold"]
    )
    watch_sec = section_html(
        "🔵 Watch — 7 to 14 Days", "#2980b9", "🔵",
        [t for t in cats["watch"] if t["status"].lower() != "on hold"]
    )
    new_sec = section_html(
        "🆕 New Tickets — Last 24 Hours", "#27ae60", "🆕",
        cats["new_today"]
    )

    return f"""<!DOCTYPE html>
<html>
<head><meta charset="UTF-8"></head>
<body style="margin:0;padding:0;background:#f4f4f4;font-family:Arial,sans-serif;">
  <table width="100%" cellpadding="0" cellspacing="0" style="background:#f4f4f4;padding:30px 0;">
    <tr><td align="center">
      <table width="680" cellpadding="0" cellspacing="0" style="max-width:680px;width:100%;">

        <!-- HEADER -->
        <tr><td style="background:#1e2330;border-radius:12px 12px 0 0;padding:24px 32px;text-align:center;">
          <div style="color:#c8a96e;font-size:24px;font-weight:700;margin-bottom:10px;">👑</div>
          <div style="color:#c8a96e;font-size:11px;letter-spacing:0.2em;text-transform:uppercase;margin-bottom:4px;">Daily Work Order Digest</div>
          <div style="color:#fff;font-size:20px;font-family:Georgia,serif;">{today_str}</div>
        </td></tr>

        <!-- STATS BAR -->
        <tr><td style="background:#c8a96e;padding:14px 32px;">
          <table width="100%" cellpadding="0" cellspacing="0">
            <tr>
              <td style="text-align:center;">
                <div style="color:#1e2330;font-size:24px;font-weight:700;">{total_active}</div>
                <div style="color:#1e2330;font-size:10px;text-transform:uppercase;letter-spacing:0.1em;">Active Tickets</div>
              </td>
              <td style="text-align:center;border-left:1px solid rgba(0,0,0,0.15);">
                <div style="color:#c0392b;font-size:24px;font-weight:700;">{len(cats['critical'])}</div>
                <div style="color:#1e2330;font-size:10px;text-transform:uppercase;letter-spacing:0.1em;">Needs Decision</div>
              </td>
              <td style="text-align:center;border-left:1px solid rgba(0,0,0,0.15);">
                <div style="color:#8e44ad;font-size:24px;font-weight:700;">{len(cats['on_hold'])}</div>
                <div style="color:#1e2330;font-size:10px;text-transform:uppercase;letter-spacing:0.1em;">On Hold</div>
              </td>
              <td style="text-align:center;border-left:1px solid rgba(0,0,0,0.15);">
                <div style="color:#27ae60;font-size:24px;font-weight:700;">{len(cats['new_today'])}</div>
                <div style="color:#1e2330;font-size:10px;text-transform:uppercase;letter-spacing:0.1em;">New Today</div>
              </td>
              <td style="text-align:center;border-left:1px solid rgba(0,0,0,0.15);">
                <div style="color:#e74c3c;font-size:24px;font-weight:700;">{len(cats['high_pri'])}</div>
                <div style="color:#1e2330;font-size:10px;text-transform:uppercase;letter-spacing:0.1em;">High Priority</div>
              </td>
            </tr>
          </table>
        </td></tr>

        <!-- BODY -->
        <tr><td style="background:#f8f8f8;padding:24px 24px 8px;">
          {critical_sec}
          {highpri_sec}
          {onhold_sec}
          {warning_sec}
          {watch_sec}
          {new_sec}
        </td></tr>

        <!-- FOOTER -->
        <tr><td style="background:#1e2330;border-radius:0 0 12px 12px;padding:16px 32px;text-align:center;">
          <div style="color:rgba(255,255,255,0.4);font-size:11px;">
            Acme Services Inc. &nbsp;·&nbsp; Generated by David &nbsp;·&nbsp; Coast Work Order Digest
          </div>
        </td></tr>

      </table>
    </td></tr>
  </table>
</body>
</html>"""


# ── SEND ─────────────────────────────────────────────────────────────────────
def send_email(subject: str, html: str):
    """Send via gog (David's proven Gmail path). To=Owner, Cc=ManagerA,ManagerB."""
    all_rcpts = [RECIPIENT] + CC_RECIPIENTS
    import tempfile
    tmp = tempfile.NamedTemporaryFile(mode='w', suffix='.html', delete=False, encoding='utf-8')
    tmp.write(html)
    tmp.close()
    env = os.environ.copy()
    env["GOG_KEYRING_PASSWORD"] = "YOUR_GOG_KEYRING_PASSWORD"
    env["GOG_KEYRING_BACKEND"]  = "file"
    cc_flags = " ".join(f'--cc "{addr}"' for addr in CC_RECIPIENTS)
    script = (f'gog gmail send --to "{RECIPIENT}" {cc_flags} '
              f'--subject "{subject}" --body-html "$(cat {tmp.name})" --account "{SENDER}"')
    result = subprocess.run(script, shell=True, env=env, capture_output=True, text=True)
    os.unlink(tmp.name)
    if result.returncode == 0:
        print(f"✓ Digest sent to {', '.join(all_rcpts)}")
    else:
        print(f"✗ Send failed: {result.stderr.strip()}")


# ── MAIN ─────────────────────────────────────────────────────────────────────
def main():
    now = datetime.now(timezone.utc)
    print(f"[coast] Starting digest at {now.strftime('%H:%M:%S')}...")

    tickets = load_tickets(CSV_PATH)
    print(f"[coast] {len(tickets)} active tickets loaded")

    if not tickets:
        print("[coast] No active tickets — skipping email")
        return

    cats = categorize(tickets)
    print(f"[coast] Critical: {len(cats['critical'])} | Warning: {len(cats['warning'])} | High Pri: {len(cats['high_pri'])} | New: {len(cats['new_today'])}")

    html    = build_html(tickets, cats, now)
    subject = f"Acme Services Work Orders — {len(tickets)} Active | {len(cats['critical'])} Need Decision — {now.strftime('%a %b %d')}"
    send_email(subject, html)
    print("[coast] Done.")


if __name__ == "__main__":
    main()
