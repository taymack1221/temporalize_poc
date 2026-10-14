import csv
import io
import base64
import os
from datetime import datetime, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.base import MIMEBase
from email import encoders

from googleapiclient.discovery import build
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request

# --- CONFIGURATION ---
TOKEN_PATH   = '/home/user/token.json'
INPUT_CSV    = '/mnt/c/ClockSharkAudit/MISSING_NOTES_REPORT.csv'
SENDER       = 'bot@example.com'
RECIPIENTS   = ['owner@example.com', 'manager2@example.com']

SCOPES = [
    'https://www.googleapis.com/auth/gmail.readonly',
    'https://www.googleapis.com/auth/spreadsheets',
    'https://mail.google.com/',
]


def get_credentials():
    creds = Credentials.from_authorized_user_file(TOKEN_PATH, SCOPES)
    if creds and creds.expired and creds.refresh_token:
        creds.refresh(Request())
        with open(TOKEN_PATH, 'w') as f:
            f.write(creds.to_json())
    return creds


def get_date_filter():
    """
    Return the set of dates to include in the report.
    - Monday: Thursday and Friday of the previous week
    - Tuesday-Friday: yesterday and the day before
    """
    today = datetime.now().date()
    weekday = today.weekday()  # Monday=0, Tuesday=1, ..., Friday=4

    if weekday == 0:  # Monday
        last_friday   = today - timedelta(days=3)
        last_thursday = today - timedelta(days=4)
        dates = {last_thursday, last_friday}
        label = f"{last_thursday.strftime('%b %d')} – {last_friday.strftime('%b %d, %Y')} (Thu/Fri)"
    else:
        # Tuesday through Friday — last 2 working days
        yesterday  = today - timedelta(days=1)
        day_before = today - timedelta(days=2)
        dates = {day_before, yesterday}
        label = f"{day_before.strftime('%b %d')} – {yesterday.strftime('%b %d, %Y')}"

    print(f"Filtering for dates: {', '.join(str(d) for d in sorted(dates))}")
    return dates, label


def filter_by_dates(input_path, target_dates):
    """Read the CSV and return header + rows matching target_dates."""
    header = None
    filtered_rows = []

    with open(input_path, newline='', encoding='utf-8-sig') as f:
        reader = csv.DictReader(f)
        header = reader.fieldnames

        for row in reader:
            start_str = row.get('Start', '').strip()
            if not start_str:
                continue
            try:
                row_date = datetime.strptime(start_str, '%m/%d/%Y %I:%M:%S %p').date()
            except ValueError:
                continue

            if row_date in target_dates:
                filtered_rows.append(row)

    return header, filtered_rows


def build_csv_attachment(header, rows):
    """Build a CSV string from header and rows."""
    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=header)
    writer.writeheader()
    writer.writerows(rows)
    return output.getvalue()


def send_email(gmail_service, csv_content, row_count, date_range):
    today_str = datetime.now().strftime('%A, %B %d, %Y')
    subject = f"ClockShark Timesheet — {date_range}"

    body_html = f"""
<div style="font-family:Arial,sans-serif; max-width:600px;">
  <div style="background:#2e2e2e; padding:16px 20px; border-radius:6px 6px 0 0;">
    <h2 style="color:white; margin:0; font-size:18px;">Acme Services — ClockShark Timesheet</h2>
    <p style="color:#aaa; margin:4px 0 0;">{today_str}</p>
  </div>
  <div style="border:1px solid #ddd; border-top:none; padding:20px; border-radius:0 0 6px 6px;">
    <p>Please find attached the ClockShark timesheet report for <strong>{date_range}</strong>.</p>
    <p><strong>{row_count} time entries</strong> are included in this report.</p>
    <hr style="border:none; border-top:1px solid #eee; margin:20px 0;">
    <p style="color:#999; font-size:12px;">— David &nbsp;|&nbsp; Acme Services Automation</p>
  </div>
</div>
"""

    msg = MIMEMultipart('mixed')
    msg['Subject'] = subject
    msg['From']    = SENDER
    msg['To']      = ', '.join(RECIPIENTS)

    msg.attach(MIMEText(body_html, 'html'))

    attachment = MIMEBase('application', 'octet-stream')
    attachment.set_payload(csv_content.encode('utf-8'))
    encoders.encode_base64(attachment)
    filename = f"ClockShark_{datetime.now().strftime('%Y%m%d')}.csv"
    attachment.add_header('Content-Disposition', 'attachment', filename=filename)
    msg.attach(attachment)

    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
    gmail_service.users().messages().send(userId='me', body={'raw': raw}).execute()
    print(f"Email sent to: {', '.join(RECIPIENTS)}")


def main():
    print(f"Starting timesheet email at {datetime.now().strftime('%H:%M:%S')}...")

    if not os.path.exists(INPUT_CSV):
        print(f"ERROR: CSV file not found at {INPUT_CSV}")
        return

    target_dates, date_range = get_date_filter()
    header, rows = filter_by_dates(INPUT_CSV, target_dates)

    if not rows:
        print(f"No entries found for {date_range}. No email sent.")
        return

    csv_content = build_csv_attachment(header, rows)

    creds = get_credentials()
    gmail = build('gmail', 'v1', credentials=creds)

    send_email(gmail, csv_content, len(rows), date_range)
    print(f"Done! {len(rows)} rows sent covering {date_range}.")


if __name__ == '__main__':
    main()
