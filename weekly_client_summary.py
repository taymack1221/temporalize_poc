#!/usr/bin/env python3
"""
Acme Services Weekly Client Summary Emailer
Runs Monday morning, reads MONTH_FRESH.csv, uses Gemini to summarize
work done at each property last week, sends a beautifully formatted email
per client group to Owner for review.

Deploy:  /home/user/weekly_client_summary.py
Cron:    0 7 * * 1  python3 /home/user/weekly_client_summary.py >> /home/user/weekly_summary.log 2>&1
"""

import csv
import os
import subprocess
import json
from datetime import datetime, date, timedelta
from collections import defaultdict
from google import genai

# ── CONFIG ────────────────────────────────────────────────────────────────────
CSV_PATH   = "/mnt/c/ClockSharkAudit/MONTH_FRESH.csv"
SENDER     = "bot@example.com"
RECIPIENT  = "owner@example.com"
GEMINI_KEY = "YOUR_GEMINI_API_KEY"
GEMINI_MODEL = "models/gemini-2.5-flash"

# Logo base64 — embedded so no hosting needed
LOGO_SRC = "https://dashboard.example.com/acme_logo.jpg"

# Client groups — keyword match on Customer: Job field
CLIENT_GROUPS = {
    "ClientA":             {"match": "clienta",             "color": "#1e3a5f", "accent": "#c8a96e", "label": "Client A"},
    "ClientB":         {"match": "clientb",          "color": "#0b3d2e", "accent": "#4caf87", "label": "Client B"},
    "ClientB North": {"match": "clientb north",  "color": "#0b3d2e", "accent": "#4caf87", "label": "ClientB North"},
    "ClientM":         {"match": "clientm",          "color": "#2d1b4e", "accent": "#9061f9", "label": "ClientM Properties"},
    "ClientN":         {"match": "clientn",          "color": "#4a1528", "accent": "#e74694", "label": "ClientN Capital"},
}

# Non-billable / internal — skip
SKIP_KEYWORDS = [
    "acme time", "sick time", "vacation", "beer time", "suspense",
    "time off", "holiday", "acme office", "acme psi",
]

# ── DATE RANGE: last Mon–Sun ──────────────────────────────────────────────────
def last_week_range() -> tuple:
    today = date.today()
    last_monday = today - timedelta(days=today.weekday() + 7)
    last_sunday  = last_monday + timedelta(days=6)
    return last_monday, last_sunday


def parse_date(s: str):
    for fmt in ["%m/%d/%Y %I:%M:%S %p", "%m/%d/%Y %H:%M", "%m/%d/%Y"]:
        try:
            return datetime.strptime(s.strip(), fmt).date()
        except ValueError:
            continue
    return None


# ── LOAD + FILTER CSV ─────────────────────────────────────────────────────────
def load_entries(week_start: date, week_end: date) -> dict:
    """
    Returns {client_group: {property_name: [note_strings]}}
    """
    result = defaultdict(lambda: defaultdict(list))

    try:
        with open(CSV_PATH, newline="", encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            for row in reader:
                customer = row.get("Customer: Job", "").strip()
                notes    = row.get("Notes", "").strip()
                start    = row.get("Start", "").strip()
                hours_s  = row.get("DecimalHours", "0").strip()
                employee = row.get("EmployeeID", "").strip()
                task     = row.get("Task", "").strip()

                if not customer or not start:
                    continue

                cl = customer.lower()
                if any(k in cl for k in SKIP_KEYWORDS):
                    continue

                entry_date = parse_date(start)
                if not entry_date:
                    continue
                if not (week_start <= entry_date <= week_end):
                    continue

                try:
                    hours = float(hours_s)
                except ValueError:
                    hours = 0.0

                if ":" in customer:
                    prop = customer.split(":", 1)[1].strip()
                else:
                    prop = customer.strip()

                group = None
                if "clientb north" in cl:
                    group = "ClientB North"
                elif "clientb" in cl:
                    group = "ClientB"
                elif "clienta" in cl:
                    group = "ClientA"
                elif "clientm" in cl:
                    group = "ClientM"
                elif "clientn" in cl:
                    group = "ClientN"

                if not group:
                    continue

                note_str = f"[{entry_date.strftime('%a %b %d')} | {employee} | {task} | {hours}h]"
                if notes:
                    note_str += f" {notes}"
                result[group][prop].append(note_str)

    except FileNotFoundError:
        print(f"ERROR: CSV not found at {CSV_PATH}")

    return result


# ── AI SUMMARY ────────────────────────────────────────────────────────────────
def summarize_property(property_name: str, entries: list, client_label: str) -> dict:
    """
    Uses Gemini to generate a narrative intro + bullet list for one property.
    Returns {"intro": str, "bullets": [str]}
    """
    entries_text = "\n".join(entries[:80])

    prompt = f"""You are David, a professional property management assistant for Acme Services Inc.
You are writing a weekly activity summary for the client "{client_label}" about work done at their property "{property_name}".

Here are the raw ClockShark time entries for this property last week:
{entries_text}

Write a summary in two parts:
1. A SHORT narrative intro (2-3 sentences max) that sounds like a professional property manager report. Mention the property name. Highlight the most significant work done. Professional but warm tone.
2. A bullet list of specific completed tasks (5-10 bullets max). Each bullet should be a clear, client-friendly description of work done. No employee names. No hours. No internal jargon. Focus on what was DONE and why it matters to the client.

Respond ONLY with valid JSON in this exact format, nothing else:
{{
  "intro": "narrative intro here",
  "bullets": ["bullet 1", "bullet 2", "bullet 3"]
}}"""

    try:
        client_api = genai.Client(api_key=GEMINI_KEY)
        response = client_api.models.generate_content(
            model=GEMINI_MODEL,
            contents=prompt
        )
        raw = response.text.strip()
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        return json.loads(raw.strip())
    except Exception as e:
        print(f"  AI error for {property_name}: {e}")
        return {
            "intro": f"Maintenance and service work was completed at {property_name} this week.",
            "bullets": [e.split("]", 1)[1].strip() if "]" in e else e for e in entries[:5]]
        }


# ── HTML EMAIL BUILDER ────────────────────────────────────────────────────────
def build_email_html(group: str, summaries: dict, week_start: date, week_end: date) -> str:
    cfg    = CLIENT_GROUPS[group]
    color  = cfg["color"]
    accent = cfg["accent"]
    label  = cfg["label"]

    week_label = f"{week_start.strftime('%B %d')} – {week_end.strftime('%B %d, %Y')}"

    props_html = ""
    for prop, data in sorted(summaries.items()):
        intro   = data.get("intro", "")
        bullets = data.get("bullets", [])
        bullets_html = "".join(
            f'<li style="margin-bottom:6px;color:#444;line-height:1.6;">{b}</li>'
            for b in bullets
        )
        props_html += f"""
        <div style="margin-bottom:28px;border-radius:10px;overflow:hidden;box-shadow:0 2px 8px rgba(0,0,0,0.07);">
          <div style="background:{color};padding:14px 20px;">
            <span style="display:inline-block;width:8px;height:8px;border-radius:50%;background:{accent};margin-right:10px;"></span>
            <span style="color:#fff;font-family:Georgia,serif;font-size:15px;font-weight:600;letter-spacing:0.03em;">{prop}</span>
          </div>
          <div style="background:#fff;padding:18px 20px;">
            <p style="color:#333;font-size:14px;line-height:1.7;margin:0 0 14px;font-style:italic;border-left:3px solid {accent};padding-left:12px;">{intro}</p>
            <ul style="margin:0;padding-left:20px;font-size:13px;">
              {bullets_html}
            </ul>
          </div>
        </div>"""

    prop_count    = len(summaries)
    total_bullets = sum(len(v.get("bullets", [])) for v in summaries.values())

    return f"""<!DOCTYPE html>
<html>
<head><meta charset="UTF-8"></head>
<body style="margin:0;padding:0;background:#f4f4f4;font-family:Arial,sans-serif;">
  <table width="100%" cellpadding="0" cellspacing="0" style="background:#f4f4f4;padding:30px 0;">
    <tr><td align="center">
      <table width="620" cellpadding="0" cellspacing="0" style="max-width:620px;width:100%;">

        <tr><td style="background:{color};border-radius:12px 12px 0 0;padding:28px 32px;text-align:center;">
          <img src="{LOGO_SRC}" alt="Acme Services Inc." width="160"
               style="display:block;margin:0 auto 16px;filter:brightness(0) invert(1);opacity:0.95;">
          <div style="color:{accent};font-size:11px;letter-spacing:0.2em;text-transform:uppercase;margin-bottom:6px;">Weekly Property Report</div>
          <div style="color:#fff;font-family:Georgia,serif;font-size:22px;font-weight:normal;margin-bottom:4px;">{label}</div>
          <div style="color:rgba(255,255,255,0.65);font-size:13px;">{week_label}</div>
        </td></tr>

        <tr><td style="background:{accent};padding:12px 32px;">
          <table width="100%" cellpadding="0" cellspacing="0">
            <tr>
              <td style="text-align:center;">
                <div style="color:{color};font-size:22px;font-weight:700;">{prop_count}</div>
                <div style="color:{color};font-size:11px;opacity:0.8;text-transform:uppercase;letter-spacing:0.1em;">Properties Serviced</div>
              </td>
              <td style="text-align:center;border-left:1px solid rgba(0,0,0,0.15);">
                <div style="color:{color};font-size:22px;font-weight:700;">{total_bullets}</div>
                <div style="color:{color};font-size:11px;opacity:0.8;text-transform:uppercase;letter-spacing:0.1em;">Completed Items</div>
              </td>
              <td style="text-align:center;border-left:1px solid rgba(0,0,0,0.15);">
                <div style="color:{color};font-size:13px;font-weight:600;">{week_start.strftime('%b %d')} – {week_end.strftime('%b %d')}</div>
                <div style="color:{color};font-size:11px;opacity:0.8;text-transform:uppercase;letter-spacing:0.1em;">Service Week</div>
              </td>
            </tr>
          </table>
        </td></tr>

        <tr><td style="background:#f8f8f8;padding:28px 28px 8px;">
          <p style="color:#666;font-size:13px;margin:0 0 20px;line-height:1.6;">
            The following is a summary of property services completed by Acme Services Inc.
            during the week of <strong>{week_label}</strong>. This report is generated automatically
            from field activity logs for your review.
          </p>
          {props_html}
        </td></tr>

        <tr><td style="background:{color};border-radius:0 0 12px 12px;padding:20px 32px;text-align:center;">
          <div style="color:rgba(255,255,255,0.5);font-size:11px;letter-spacing:0.05em;">
            Acme Services Inc. &nbsp;·&nbsp; Internal Review Copy &nbsp;·&nbsp; Not for client distribution
          </div>
          <div style="color:rgba(255,255,255,0.3);font-size:10px;margin-top:4px;">
            Generated by David · Acme Services Automation
          </div>
        </td></tr>

      </table>
    </td></tr>
  </table>
</body>
</html>"""


# ── SEND EMAIL ────────────────────────────────────────────────────────────────
def send_email(subject: str, html: str):
    import tempfile
    tmp = tempfile.NamedTemporaryFile(mode='w', suffix='.html', delete=False, encoding='utf-8')
    tmp.write(html)
    tmp.close()

    env = os.environ.copy()
    env["GOG_KEYRING_PASSWORD"] = "YOUR_GOG_KEYRING_PASSWORD"

    # Read file contents and pass via --body-html
    with open(tmp.name, 'r') as f:
        html_content = f.read()

    # Use gog via stdin workaround
    script = f"""GOG_KEYRING_PASSWORD=YOUR_GOG_KEYRING_PASSWORD gog gmail send \
        --to "{RECIPIENT}" \
        --subject "{subject}" \
        --body-html "$(cat {tmp.name})" \
        --account "{SENDER}" """

    result = subprocess.run(script, shell=True, env=env, capture_output=True, text=True)
    os.unlink(tmp.name)
    if result.returncode == 0:
        print(f"  ✓ Sent: {subject}")
    else:
        print(f"  ✗ Failed: {result.stderr}")


# ── MAIN ──────────────────────────────────────────────────────────────────────
def main():
    week_start, week_end = last_week_range()
    print(f"[weekly] Week: {week_start} → {week_end}")

    all_entries = load_entries(week_start, week_end)
    print(f"[weekly] Client groups found: {list(all_entries.keys())}")

    for group, properties in all_entries.items():
        if not properties:
            continue
        print(f"\n[weekly] Building {group} summary ({len(properties)} properties)...")

        summaries = {}
        for prop, entries in sorted(properties.items()):
            print(f"  → {prop} ({len(entries)} entries)")
            cfg = CLIENT_GROUPS.get(group, {})
            summaries[prop] = summarize_property(prop, entries, cfg.get("label", group))

        html = build_email_html(group, summaries, week_start, week_end)
        subject = f"[REVIEW] Acme Services Weekly Summary — {group} — {week_start.strftime('%b %d')}–{week_end.strftime('%b %d, %Y')}"
        send_email(subject, html)

    print("\n[weekly] Done.")


if __name__ == "__main__":
    main()
