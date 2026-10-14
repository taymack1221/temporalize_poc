#!/usr/bin/env python3
"""
Acme Services Daily Billing Summary
Reads MONTH_FRESH.csv (or 15DAY_FRESH.csv), calculates approximate billable
amounts by client group, and emails a Day / Week / Month summary to ManagerA & ManagerB.

Deploy to: /home/user/billing_summary.py
Cron:      0 17 * * 1-5  python3 /home/user/billing_summary.py >> /home/user/billing_summary.log 2>&1
"""

import csv
import os
import json
import base64
import subprocess
from datetime import datetime, date, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.encoders import encode_base64
import sys

# ── CONFIG ────────────────────────────────────────────────────────────────────
CSV_PATH   = "/mnt/c/ClockSharkAudit/MONTH_FRESH.csv"
SENDER     = "bot@example.com"
RECIPIENTS = ["manager1@example.com", "manager2@example.com", "owner@example.com"]
CREDS_PATH = "/home/user/.gmail_token.json"
GOG        = "GOG_KEYRING_PASSWORD=YOUR_GOG_KEYRING_PASSWORD /usr/local/bin/gog"

# Over-10-hour flag threshold
MAX_HOURS  = 10.0

# ── EMPLOYEE RATES (ClientA only — rate varies per person) ────────────────────────
ClientA_RATES = {
    "Employee 02": 50,
    "Employee 07": 50,
    "Employee 13": 50,
    "Owner Smith": 50,
    "Employee 16": 50,
    "Employee 15": 50,
    "Employee 04": 50,
    "Employee 09": 50,
    "Employee 03": 50,
    "Employee 05": 50,
    "Employee 11": 50,
    "Employee 06": 50,
    "Employee 12": 50,
    "Employee 14": 50,
    "Employee 10": 50,
    "Employee 08": 50,
}

# Non-billable customer keywords — skip entirely
NON_BILLABLE = [
    "internal time", "sick time", "vacation", "beer time", "suspense",
    "time off", "holiday", "acme office", "acme psi",
]

def classify_customer(customer: str, task: str) -> tuple[str, float]:
    """
    Returns (group_label, hourly_rate) given the Customer:Job string and Task.
    Returns ("", 0) for non-billable entries.
    """
    c = customer.strip().lower()
    t = task.strip().upper()

    # Skip non-billable
    for nb in NON_BILLABLE:
        if nb in c:
            return ("", 0)

    is_hvac = (t == "HVAC")

    if c.startswith("clienta"):
        return ("ClientA", None)           # None = per-employee rate lookup

    if c.startswith("clientb north"):
        return ("ClientB North", 70)

    if c.startswith("clientb"):
        rate = 100 if is_hvac else 80
        return ("ClientB", rate)

    if c.startswith("clientm"):
        rate = 100 if is_hvac else 80
        return ("ClientM", rate)

    if c.startswith("clientn"):
        rate = 100 if is_hvac else 80
        return ("ClientN", rate)

    # Everything else — skip (EP 2 Door Move, project-specific codes, etc.)
    # You can add more clients here as needed
    return ("Other", 0)


def parse_csv(path: str) -> list[dict]:
    rows = []
    try:
        with open(path, newline="", encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            for row in reader:
                rows.append(row)
    except FileNotFoundError:
        print(f"ERROR: CSV not found at {path}")
    return rows


def parse_date(start_str: str) -> date | None:
    for fmt in ["%m/%d/%Y %I:%M:%S %p", "%m/%d/%Y %H:%M", "%m/%d/%Y"]:
        try:
            return datetime.strptime(start_str.strip(), fmt).date()
        except ValueError:
            continue
    return None


def process_rows(rows: list[dict]) -> tuple[list[dict], list[dict]]:
    """
    Returns (entries, flags)
    entries: list of processed billing entries
    flags:   list of rows flagged for >10 hours
    """
    entries = []
    flags   = []

    for row in rows:
        emp_id   = row.get("EmployeeID", "").strip()
        customer = row.get("Customer: Job", "").strip()
        task     = row.get("Task", "").strip()
        start    = row.get("Start", "").strip()
        hours_s  = row.get("DecimalHours", "0").strip()

        if not customer or not start:
            continue

        try:
            hours = float(hours_s)
        except ValueError:
            hours = 0.0

        entry_date = parse_date(start)
        if not entry_date:
            continue

        group, rate = classify_customer(customer, task)
        if not group:
            continue  # non-billable

        # ClientA: look up per-employee rate
        if group == "ClientA":
            rate = ClientA_RATES.get(emp_id, 0)
            if rate == 0:
                # Unknown employee — skip or use 0
                continue

        # Other with rate 0 — skip
        if rate == 0:
            continue

        flagged = hours > MAX_HOURS
        if flagged:
            flags.append({
                "employee": emp_id,
                "customer": customer,
                "date":     entry_date.strftime("%a %b %d"),
                "hours":    round(hours, 2),
            })

        entries.append({
            "employee":   emp_id,
            "customer":   customer,
            "group":      group,
            "task":       task,
            "date":       entry_date,
            "hours":      round(hours, 2),
            "rate":       rate,
            "amount":     round(hours * rate, 2),
            "flagged":    flagged,
        })

    return entries, flags


def date_ranges() -> dict:
    today = date.today()
    # Week: Mon–today
    week_start = today - timedelta(days=today.weekday())
    # Month: 1st of current month
    month_start = today.replace(day=1)
    return {
        "today":  today,
        "week":   week_start,
        "month":  month_start,
    }


def summarize(entries: list[dict], ranges: dict) -> dict:
    today      = ranges["today"]
    week_start = ranges["week"]
    month_start= ranges["month"]

    def bucket(e):
        d = e["date"]
        buckets = []
        if d == today:
            buckets.append("today")
        if d >= week_start:
            buckets.append("week")
        if d >= month_start:
            buckets.append("month")
        return buckets

    # totals per period
    totals = {
        "today": {"hours": 0.0, "amount": 0.0},
        "week":  {"hours": 0.0, "amount": 0.0},
        "month": {"hours": 0.0, "amount": 0.0},
    }
    # by group per period
    by_group = {
        "today": {}, "week": {}, "month": {}
    }

    for e in entries:
        for b in bucket(e):
            totals[b]["hours"]  += e["hours"]
            totals[b]["amount"] += e["amount"]
            g = e["group"]
            if g not in by_group[b]:
                by_group[b][g] = {"hours": 0.0, "amount": 0.0}
            by_group[b][g]["hours"]  += e["hours"]
            by_group[b][g]["amount"] += e["amount"]

    # round
    for b in totals:
        totals[b]["hours"]  = round(totals[b]["hours"], 2)
        totals[b]["amount"] = round(totals[b]["amount"], 2)
        for g in by_group[b]:
            by_group[b][g]["hours"]  = round(by_group[b][g]["hours"], 2)
            by_group[b][g]["amount"] = round(by_group[b][g]["amount"], 2)

    return {"totals": totals, "by_group": by_group}


def fmt_currency(n: float) -> str:
    return f"${n:,.2f}"

def fmt_hours(n: float) -> str:
    return f"{n:,.1f} hrs"


def build_html(summary: dict, flags: list[dict], ranges: dict) -> str:
    today  = ranges["today"]
    week_s = ranges["week"]
    month_s= ranges["month"]

    def period_label(key):
        if key == "today":
            return today.strftime("%A, %B %d")
        elif key == "week":
            return f"Week of {week_s.strftime('%b %d')}"
        else:
            return today.strftime("%B %Y")

    GROUP_ORDER = ["ClientA", "ClientB", "ClientB North", "ClientM", "ClientN", "Other"]
    GROUP_COLOR = {
        "ClientA":            "#1a56db",
        "ClientB":        "#0694a2",
        "ClientB North":"#057a55",
        "ClientM":        "#9061f9",
        "ClientN":        "#e74694",
        "Other":          "#6b7280",
    }

    def period_block(key):
        t  = summary["totals"][key]
        bg = summary["by_group"][key]
        label = period_label(key)
        rows_html = ""
        for g in GROUP_ORDER:
            if g in bg:
                color = GROUP_COLOR.get(g, "#6b7280")
                rows_html += f"""
          <tr>
            <td style="padding:6px 12px;border-bottom:1px solid #f0f0f0;">
              <span style="display:inline-block;width:10px;height:10px;border-radius:50%;background:{color};margin-right:6px;vertical-align:middle;"></span>
              {g}
            </td>
            <td style="padding:6px 12px;border-bottom:1px solid #f0f0f0;text-align:right;color:#555;">{fmt_hours(bg[g]['hours'])}</td>
            <td style="padding:6px 12px;border-bottom:1px solid #f0f0f0;text-align:right;font-weight:600;">{fmt_currency(bg[g]['amount'])}</td>
          </tr>"""

        return f"""
      <div style="margin-bottom:28px;border:1px solid #e5e7eb;border-radius:10px;overflow:hidden;">
        <div style="background:#1e2330;padding:12px 16px;">
          <span style="color:#fff;font-weight:600;font-size:15px;">{label}</span>
          <span style="float:right;color:#c8a96e;font-weight:700;font-size:16px;">{fmt_currency(t['amount'])}</span>
        </div>
        <table style="width:100%;border-collapse:collapse;font-size:13px;font-family:Arial,sans-serif;">
          <tr style="background:#f9fafb;">
            <th style="padding:8px 12px;text-align:left;font-weight:600;color:#374151;border-bottom:2px solid #e5e7eb;">Client Group</th>
            <th style="padding:8px 12px;text-align:right;font-weight:600;color:#374151;border-bottom:2px solid #e5e7eb;">Hours</th>
            <th style="padding:8px 12px;text-align:right;font-weight:600;color:#374151;border-bottom:2px solid #e5e7eb;">Est. Billing</th>
          </tr>
          {rows_html}
          <tr style="background:#f9fafb;">
            <td style="padding:8px 12px;font-weight:700;">Total</td>
            <td style="padding:8px 12px;text-align:right;font-weight:700;">{fmt_hours(t['hours'])}</td>
            <td style="padding:8px 12px;text-align:right;font-weight:700;color:#1a56db;">{fmt_currency(t['amount'])}</td>
          </tr>
        </table>
      </div>"""

    flags_section = ""
    if flags:
        flag_rows = ""
        for f in flags:
            flag_rows += f"""
          <tr>
            <td style="padding:6px 12px;border-bottom:1px solid #f0f0f0;">{f['employee']}</td>
            <td style="padding:6px 12px;border-bottom:1px solid #f0f0f0;">{f['date']}</td>
            <td style="padding:6px 12px;border-bottom:1px solid #f0f0f0;">{f['customer']}</td>
            <td style="padding:6px 12px;border-bottom:1px solid #f0f0f0;text-align:right;color:#c84a4a;font-weight:600;">{f['hours']} hrs ⚠</td>
          </tr>"""

        flags_section = f"""
      <div style="margin-top:20px;border:1px solid #fca5a5;border-radius:10px;overflow:hidden;">
        <div style="background:#fef2f2;padding:10px 16px;border-bottom:1px solid #fca5a5;">
          <span style="color:#c84a4a;font-weight:600;">⚠ Possible Missed Punch-Outs ({len(flags)} entries over {MAX_HOURS} hrs)</span>
        </div>
        <table style="width:100%;border-collapse:collapse;font-size:13px;font-family:Arial,sans-serif;">
          <tr style="background:#f9fafb;">
            <th style="padding:8px 12px;text-align:left;font-weight:600;color:#374151;border-bottom:1px solid #e5e7eb;">Employee</th>
            <th style="padding:8px 12px;text-align:left;font-weight:600;color:#374151;border-bottom:1px solid #e5e7eb;">Date</th>
            <th style="padding:8px 12px;text-align:left;font-weight:600;color:#374151;border-bottom:1px solid #e5e7eb;">Customer</th>
            <th style="padding:8px 12px;text-align:right;font-weight:600;color:#374151;border-bottom:1px solid #e5e7eb;">Hours</th>
          </tr>
          {flag_rows}
        </table>
        <div style="padding:10px 16px;background:#fef2f2;font-size:12px;color:#7f1d1d;">
          These entries are included in billing totals but should be verified in ClockShark.
        </div>
      </div>"""

    today_str = today.strftime("%A, %B %d, %Y")
    return f"""
<div style="font-family:Arial,sans-serif;max-width:640px;">
  <div style="background:#1e2330;padding:18px 20px;border-radius:10px 10px 0 0;">
    <h2 style="color:#fff;margin:0;font-size:18px;">Acme Services — Billing Tracker</h2>
    <p style="color:#aaa;margin:4px 0 0;font-size:13px;">{today_str} &nbsp;|&nbsp; Approximate billable labor</p>
  </div>
  <div style="border:1px solid #e5e7eb;border-top:none;padding:20px;border-radius:0 0 10px 10px;background:#fff;">
    <p style="font-size:12px;color:#9ca3af;margin:0 0 18px;">
      Based on ClockShark decimal hours × client billing rates. Excludes Acme Time, Sick, Vacation.
      ClientA rates are per-employee. ClientB HVAC tasks billed at $100/hr, all others $80/hr.
      ClientB North flat $70/hr. ClientM/ClientN: HVAC $100/hr, all others $80/hr.
    </p>
    {period_block('today')}
    {period_block('week')}
    {period_block('month')}
    {flags_section}
    <hr style="border:none;border-top:1px solid #eee;margin:20px 0;">
    <p style="color:#999;font-size:12px;margin:0;">— David &nbsp;|&nbsp; Acme Services Automation &nbsp;|&nbsp; Approximate figures only</p>
  </div>
</div>"""


def send_email(html: str, today: date):
    subject = f"Acme Services Billing Tracker — {today.strftime('%a %b %d, %Y')}"
    msg = MIMEMultipart("mixed")
    msg["Subject"] = subject
    msg["From"]    = SENDER
    msg["To"]      = ", ".join(RECIPIENTS)
    msg.attach(MIMEText(html, "html"))

    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()

    cmd = [
        "gog", "gmail", "send",
        "--to", ", ".join(RECIPIENTS),
        "--subject", subject,
        "--body-html", html,
        "--account", SENDER,
    ]
    env = os.environ.copy()
    env["GOG_KEYRING_PASSWORD"] = "YOUR_GOG_KEYRING_PASSWORD"
    result = subprocess.run(cmd, env=env, capture_output=True, text=True)
    if result.returncode == 0:
        print(f"Email sent to {', '.join(RECIPIENTS)}")
    else:
        print(f"Email send failed: {result.stderr}")
        # Fallback: try raw gmail API
        sys.exit(1)


def main():
    print(f"[billing] Starting at {datetime.now().strftime('%H:%M:%S')}...")
    rows    = parse_csv(CSV_PATH)
    print(f"[billing] {len(rows)} rows loaded from CSV")

    entries, flags = process_rows(rows)
    print(f"[billing] {len(entries)} billable entries, {len(flags)} flagged")

    ranges  = date_ranges()
    summary = summarize(entries, ranges)

    t = summary["totals"]
    print(f"[billing] Today: {fmt_currency(t['today']['amount'])} | "
          f"Week: {fmt_currency(t['week']['amount'])} | "
          f"Month: {fmt_currency(t['month']['amount'])}")

    html = build_html(summary, flags, ranges)
    send_email(html, ranges["today"])
    print(f"[billing] Done.")


if __name__ == "__main__":
    main()
