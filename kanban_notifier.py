import json
import os
import base64
import hashlib
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from googleapiclient.discovery import build
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request

# --- CONFIGURATION ---
SHEET_ID = 'YOUR_SHEET_ID'
SHEET_NAME = 'Sheet1'
TOKEN_PATH = '/home/user/token.json'
SNAPSHOT_PATH = '/home/user/kanban_snapshot.json'
KANBAN_URL = 'https://dashboard.example.com/kanban.php'

SCOPES = [
    'https://www.googleapis.com/auth/gmail.readonly',
    'https://www.googleapis.com/auth/spreadsheets.readonly',
    'https://mail.google.com/',
    'https://www.googleapis.com/auth/calendar.readonly',
]

# Only these people get email notifications
EMAIL_MAP = {
    'managera':     'manager1@example.com',
    'managerb':      'manager2@example.com',
    'owner':     'owner@example.com',
    'owner':  'owner@example.com',
}

SENDER = 'bot@example.com'


def get_credentials():
    creds = Credentials.from_authorized_user_file(TOKEN_PATH, SCOPES)
    if creds and creds.expired and creds.refresh_token:
        creds.refresh(Request())
        with open(TOKEN_PATH, 'w') as f:
            f.write(creds.to_json())
    return creds


def get_sheet_data(creds):
    import subprocess
    cmd = f"GOG_KEYRING_PASSWORD=YOUR_GOG_KEYRING_PASSWORD /usr/local/bin/gog sheets get {SHEET_ID} 'Sheet1!A2:G200' --account owner@example.com --json"
    result = subprocess.check_output(cmd, shell=True, executable='/bin/bash', timeout=30).decode('utf-8')
    parsed = json.loads(result)
    rows = parsed.get('values', []) if isinstance(parsed, dict) else parsed

    cards = {}
    for i, row in enumerate(rows):
        while len(row) < 7:
            row.append('')
        project, status, priority, assigned_to, due_date, notes, photo = row[:7]
        if not project.strip():
            continue
        card_key = project.strip()
        cards[card_key] = {
            'project':     project.strip(),
            'status':      status.strip(),
            'priority':    priority.strip(),
            'assigned_to': assigned_to.strip(),
            'due_date':    due_date.strip(),
            'notes':       notes.strip(),
            'row':         i + 2,
        }
    return cards


def load_snapshot():
    if not os.path.exists(SNAPSHOT_PATH):
        return {}
    with open(SNAPSHOT_PATH, 'r') as f:
        return json.load(f)


def save_snapshot(cards):
    with open(SNAPSHOT_PATH, 'w') as f:
        json.dump(cards, f, indent=2)


def get_recipients(assigned_to_str):
    """Parse assigned_to field and return list of emails for notifiable people."""
    if not assigned_to_str:
        return []

    # Split on common delimiters: comma, slash, ampersand
    import re
    names = re.split(r'[,/&]+', assigned_to_str)
    emails = []
    for name in names:
        name_clean = name.strip().lower()
        if name_clean in EMAIL_MAP:
            email = EMAIL_MAP[name_clean]
            if email not in emails:
                emails.append(email)
    return emails


def send_email(gmail_service, to_emails, subject, body_html):
    if not to_emails:
        return

    msg = MIMEMultipart('alternative')
    msg['Subject'] = subject
    msg['From'] = SENDER
    msg['To'] = ', '.join(to_emails)
    msg.attach(MIMEText(body_html, 'html'))

    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
    gmail_service.users().messages().send(userId='me', body={'raw': raw}).execute()
    print(f"  Email sent to: {', '.join(to_emails)}")


def format_card_email(card, change_type, changes=None):
    """Build the HTML email body for a card notification."""
    today_str = datetime.now().strftime('%A, %B %d, %Y')

    if change_type == 'new':
        header_color = '#1a73e8'
        header_label = '🆕 New Card Added'
        subject = f"New Project Card: {card['project']}"
    else:
        header_color = '#f9a825'
        header_label = '✏️ Card Updated'
        subject = f"Project Update: {card['project']}"

    # Build changes table if applicable
    changes_html = ''
    if changes:
        rows_html = ''
        for field, (old_val, new_val) in changes.items():
            rows_html += f"""
            <tr>
                <td style="padding:8px 12px; font-weight:bold; color:#555; white-space:nowrap;">{field}</td>
                <td style="padding:8px 12px; color:#e53935; text-decoration:line-through;">{old_val or '—'}</td>
                <td style="padding:8px 12px; color:#2e7d32; font-weight:bold;">{new_val or '—'}</td>
            </tr>"""
        changes_html = f"""
        <h3 style="color:#555; font-size:13px; margin:20px 0 8px; text-transform:uppercase; letter-spacing:0.05em;">What Changed</h3>
        <table style="width:100%; border-collapse:collapse; background:#f9f9f9; border-radius:6px; overflow:hidden;">
            <thead>
                <tr style="background:#eee;">
                    <th style="padding:8px 12px; text-align:left; font-size:12px; color:#777;">Field</th>
                    <th style="padding:8px 12px; text-align:left; font-size:12px; color:#777;">Before</th>
                    <th style="padding:8px 12px; text-align:left; font-size:12px; color:#777;">After</th>
                </tr>
            </thead>
            <tbody>{rows_html}</tbody>
        </table>"""

    body_html = f"""
<div style="font-family:Arial,sans-serif; max-width:620px;">
  <div style="background:{header_color}; padding:16px 20px; border-radius:6px 6px 0 0;">
    <h2 style="color:white; margin:0; font-size:17px;">{header_label}</h2>
    <p style="color:rgba(255,255,255,0.8); margin:4px 0 0; font-size:13px;">{today_str}</p>
  </div>
  <div style="border:1px solid #ddd; border-top:none; padding:20px; border-radius:0 0 6px 6px;">

    <h2 style="margin:0 0 16px; font-size:18px; color:#222;">{card['project']}</h2>

    <table style="width:100%; border-collapse:collapse; margin-bottom:16px;">
      <tr>
        <td style="padding:6px 0; color:#777; font-size:13px; width:110px;">Status</td>
        <td style="padding:6px 0; font-weight:bold;">{card['status'] or '—'}</td>
      </tr>
      <tr>
        <td style="padding:6px 0; color:#777; font-size:13px;">Priority</td>
        <td style="padding:6px 0; font-weight:bold;">{card['priority'] or '—'}</td>
      </tr>
      <tr>
        <td style="padding:6px 0; color:#777; font-size:13px;">Assigned To</td>
        <td style="padding:6px 0; font-weight:bold;">{card['assigned_to'] or '—'}</td>
      </tr>
      <tr>
        <td style="padding:6px 0; color:#777; font-size:13px;">Due Date</td>
        <td style="padding:6px 0; font-weight:bold;">{card['due_date'] or '—'}</td>
      </tr>
      <tr>
        <td style="padding:6px 0; color:#777; font-size:13px; vertical-align:top;">Notes</td>
        <td style="padding:6px 0;">{card['notes'] or '—'}</td>
      </tr>
    </table>

    {changes_html}

    <p style="text-align:center; margin:24px 0 8px;">
      <a href="{KANBAN_URL}"
         style="background:#1a73e8; color:white; padding:11px 26px;
                border-radius:4px; text-decoration:none; font-weight:bold; font-size:14px;">
        Open Project Board
      </a>
    </p>

    <hr style="border:none; border-top:1px solid #eee; margin:20px 0;">
    <p style="color:#999; font-size:12px;">— David &nbsp;|&nbsp; Acme Services Automation</p>
  </div>
</div>
"""
    return subject, body_html


def detect_changes(old_card, new_card):
    """Return dict of changed fields: {field_label: (old_val, new_val)}"""
    watched_fields = {
        'status':      'Status',
        'priority':    'Priority',
        'notes':       'Notes',
        'assigned_to': 'Assigned To',
    }
    changes = {}
    for field, label in watched_fields.items():
        if old_card.get(field, '') != new_card.get(field, ''):
            changes[label] = (old_card.get(field, ''), new_card.get(field, ''))
    return changes


def main():
    print(f"Kanban notifier running at {datetime.now().strftime('%H:%M:%S')}...")

    creds = get_credentials()
    gmail = build('gmail', 'v1', credentials=creds)

    current_cards = get_sheet_data(creds)
    snapshot = load_snapshot()

    notifications_sent = 0

    # Check for new cards and changed cards
    for card_key, card in current_cards.items():
        if card_key not in snapshot:
            # New card
            recipients = get_recipients(card['assigned_to'])
            if recipients:
                subject, body = format_card_email(card, 'new')
                send_email(gmail, recipients, subject, body)
                notifications_sent += 1
                print(f"  New card: {card_key}")
        else:
            # Existing card — check for changes
            changes = detect_changes(snapshot[card_key], card)
            if changes:
                # Use new assignee if assignee changed, otherwise current
                recipients = get_recipients(card['assigned_to'])
                if recipients:
                    subject, body = format_card_email(card, 'updated', changes)
                    send_email(gmail, recipients, subject, body)
                    notifications_sent += 1
                    print(f"  Updated card: {card_key} — {list(changes.keys())}")

    # Save updated snapshot
    save_snapshot(current_cards)

    if notifications_sent == 0:
        print("  No changes detected.")
    else:
        print(f"  {notifications_sent} notification(s) sent.")


if __name__ == '__main__':
    main()
