# -*- coding: utf-8 -*-
import csv
import os
import subprocess
import requests
from datetime import datetime, timedelta
from time import sleep

INPUT_CSV = "/mnt/c/ClockSharkAudit/MISSING_NOTES_REPORT.csv"
OUTPUT_CSV = "/mnt/c/ClockSharkAudit/PAR_AUDIT_RECOMMENDATIONS.csv"
GOOGLE_API_KEY = os.environ.get('GOOGLE_API_KEY', 'YOUR_GEMINI_API_KEY')
CUTOFF_DAYS = 2

env = os.environ.copy()
env['GOG_KEYRING_PASSWORD'] = 'YOUR_GOG_KEYRING_PASSWORD'

def clean_text(text):
    text = str(text)
    replacements = [
        ('\xe2\x80\x99', "'"),
        ('\xe2\x80\x9c', '"'),
        ('\xe2\x80\x9d', '"'),
        ('\xe2\x80\x93', '-'),
        ('\xe2\x80\x94', '-'),
        ('\xc3\xa9', 'e'),
        ('\xc3\xa8', 'e'),
    ]
    for bad, good in replacements:
        text = text.replace(bad, good)
    # Also handle the garbled versions
    text = text.replace('â\x80\x99', "'")
    text = text.replace('â\x80\x9c', '"')
    text = text.replace('â\x80\x9d', '"')
    text = text.replace('â\x80\x93', '-')
    text = text.replace('â\x80\x94', '-')
    return text.strip()

def evaluate_note_with_ai(employee_name, job, task, start, end, note):
    note = clean_text(note)
    if not note:
        return "CRITICAL", "Note is completely blank. Employee must document work performed using Problem-Action-Result format."

    prompt = """You are David, Lead Auditor for Acme Services (Acme Services).
You are reviewing maintenance shift notes through the eyes of an experienced building engineer and HVAC technician.
Evaluate this note with MODERATE strictness - only flag clear problems.

Employee: """ + employee_name + """
Location/Job: """ + job + """
Task: """ + task + """
Shift: """ + start + """ to """ + end + """
Note: """ + note + """

Rate as GOOD, FAIR, POOR, or CRITICAL.

GOOD = Clear, professional, describes work performed adequately
FAIR = Acceptable but missing result or uses vague terminology
POOR = Too vague, too short, or missing critical information
CRITICAL = Blank or completely meaningless

Reply with ONLY four lines, exactly like this:
QUALITY: GOOD
RECOMMENDATION: Your recommendation here."""

    url = "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key=" + GOOGLE_API_KEY

    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.1, "maxOutputTokens": 1000}
    }

    try:
        response = requests.post(url, json=payload, timeout=30)
        result = response.json()
        text = result['candidates'][0]['content']['parts'][0]['text'].strip()

        quality = "ERROR"
        recommendation = "Could not parse response"

        for line in text.split('\n'):
            line = line.strip()
            if line.upper().startswith('QUALITY:'):
                q = line.split(':', 1)[1].strip().upper()
                if q in ['GOOD', 'FAIR', 'POOR', 'CRITICAL']:
                    quality = q
            elif line.upper().startswith('RECOMMEND'):
                recommendation = line.split(':', 1)[1].strip()

        return quality, recommendation

    except Exception as e:
        return "ERROR", "API error: " + str(e)

def run_audit():
    print("[" + str(datetime.now()) + "] Starting AI-Powered PAR Audit (last " + str(CUTOFF_DAYS) + " days)...")

    cutoff = datetime.now() - timedelta(days=CUTOFF_DAYS)

    rows_processed = 0
    rows_skipped = 0
    issues_found = 0
    critical = poor = fair = good = error = 0

    all_rows = []
    fieldnames = None

    with open(INPUT_CSV, 'r', encoding='utf-8-sig') as infile:
        reader = csv.DictReader(infile)
        fieldnames = list(reader.fieldnames) + ['Audit_Quality', 'David_Recommendation']

        for row in reader:
            try:
                start_dt = datetime.strptime(row['Start'].strip(), '%m/%d/%Y %I:%M:%S %p')
            except:
                row['Audit_Quality'] = 'SKIPPED'
                row['David_Recommendation'] = 'Could not parse date'
                rows_skipped += 1
                all_rows.append(row)
                continue

            if start_dt < cutoff:
                row['Audit_Quality'] = 'SKIPPED'
                row['David_Recommendation'] = 'Outside 7-day review window'
                rows_skipped += 1
                all_rows.append(row)
                continue

            rows_processed += 1
            employee = (row.get('EmployeeFName', '') + ' ' + row.get('EmployeeLName', '')).strip()
            job = row.get('Customer: Job', '')
            task = row.get('Task', '')
            note = row.get('Notes', '')

            print("  [" + str(rows_processed) + "] " + employee + " - " + job[:35] + "...")

            quality, recommendation = evaluate_note_with_ai(
                employee, job, task, row['Start'], row['End'], note
            )

            row['Audit_Quality'] = quality
            row['David_Recommendation'] = recommendation
            all_rows.append(row)

            if quality == 'CRITICAL': critical += 1; issues_found += 1
            elif quality == 'POOR': poor += 1; issues_found += 1
            elif quality == 'FAIR': fair += 1; issues_found += 1
            elif quality == 'GOOD': good += 1
            elif quality == 'ERROR': error += 1

            sleep(0.3)

    with open(OUTPUT_CSV, 'w', newline='', encoding='utf-8') as outfile:
        writer = csv.DictWriter(outfile, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(all_rows)

    print("\n[" + str(datetime.now()) + "] Audit Complete!")
    print("  Reviewed: " + str(rows_processed) + " | Skipped: " + str(rows_skipped))
    print("  GOOD: " + str(good) + " | FAIR: " + str(fair) + " | POOR: " + str(poor) + " | CRITICAL: " + str(critical) + " | ERROR: " + str(error))

    send_report(rows_processed, issues_found, critical, poor, fair, good)

def send_report(total, issues, critical, poor, fair, good):
    body = ("Boss,\n\n"
            "AI-Powered PAR Method Audit is complete. Results for the last " + str(CUTOFF_DAYS) + " days:\n\n"
            "Total Shifts Reviewed: " + str(total) + "\n"
            "Total Issues Found: " + str(issues) + "\n\n"
            "Breakdown:\n"
            "  GOOD (Meets standards): " + str(good) + "\n"
            "  FAIR (Could be improved): " + str(fair) + "\n"
            "  POOR (Too vague/short): " + str(poor) + "\n"
            "  CRITICAL (Blank/missing): " + str(critical) + "\n\n"
            "Each note evaluated by David through the lens of a building engineer.\n"
            "Full audit report with recommendations is attached.\n\n"
            "- David\nAcme Services Lead Auditor")

    cmd = [
        'gog', 'gmail', 'send',
        '--to', 'owner@example.com',
        '--subject', 'PAR Method Audit Results - ' + datetime.now().strftime("%m/%d/%Y"),
        '--body', body,
        '--account', 'bot@example.com',
        '--cc', 'manager1@example.com,manager2@example.com',
        '--attach', OUTPUT_CSV
    ]

    print("\n[" + str(datetime.now()) + "] Sending email with attachment...")
    result = subprocess.run(cmd, capture_output=True, text=True, env=env)
    if result.returncode == 0:
        print("Email sent successfully!")
    else:
        print("Email failed: " + result.stderr)

if __name__ == "__main__":
    run_audit()
