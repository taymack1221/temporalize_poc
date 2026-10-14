# -*- coding: utf-8 -*-
"""
par_audit_15day.py
Run manually on demand.
1. Downloads a fresh ClockShark CSV
2. Filters to last 15 days
3. Runs PAR audit via Gemini AI
4. Emails formatted Excel results to ManagerB, ManagerA, Owner
"""
import csv
import os
import glob
import shutil
import subprocess
import requests
import time
import openpyxl
from openpyxl.styles import PatternFill, Font, Alignment, Border, Side
from openpyxl.utils import get_column_letter
from datetime import datetime, timedelta
from dotenv import load_dotenv
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.common.action_chains import ActionChains

# --- CONFIGURATION ---
load_dotenv('/mnt/c/ClockSharkAudit/.env')
EMAIL = os.getenv('CLOCKSHARK_EMAIL')
PASSWORD = os.getenv('CLOCKSHARK_PASSWORD')
DOWNLOAD_DIR = '/mnt/c/ClockSharkAudit/downloads'
FRESH_CSV = '/mnt/c/ClockSharkAudit/15DAY_FRESH.csv'
OUTPUT_XLSX = '/mnt/c/ClockSharkAudit/PAR_AUDIT_15DAY_{}.xlsx'.format(datetime.now().strftime('%Y-%m-%d'))
GOOGLE_API_KEY = os.environ.get('GOOGLE_API_KEY', 'YOUR_GEMINI_API_KEY')
CUTOFF_DAYS = 15

KEEP_COLUMNS = [
    'EmployeeID',
    'Customer: Job',
    'Task',
    'Start',
    'DecimalHours',
    'Notes',
    'Audit_Quality',
    'David_Recommendation'
]

env = os.environ.copy()
env['GOG_KEYRING_PASSWORD'] = 'YOUR_GOG_KEYRING_PASSWORD'


# --- STEP 1: DOWNLOAD FRESH CSV ---
def download_fresh_csv():
    print(f"[{datetime.now()}] Downloading fresh ClockShark CSV (last 14 days)...")
    os.makedirs(DOWNLOAD_DIR, exist_ok=True)

    options = Options()
    options.add_argument('--headless')
    options.add_argument('--no-sandbox')
    options.add_argument('--disable-dev-shm-usage')
    options.add_argument('--disable-gpu')
    options.add_argument('--window-size=1920,1080')
    prefs = {
        'download.default_directory': DOWNLOAD_DIR,
        'download.prompt_for_download': False,
        'download.directory_upgrade': True,
        'safebrowsing.enabled': True
    }
    options.add_experimental_option('prefs', prefs)

    driver = webdriver.Chrome(options=options)
    wait = WebDriverWait(driver, 30)

    try:
        driver.get('https://app.clockshark.com/Login')
        wait.until(lambda d: len(d.find_elements(By.TAG_NAME, 'input')) >= 2)
        time.sleep(2)
        inputs = driver.find_elements(By.TAG_NAME, 'input')
        ActionChains(driver).click(inputs[0]).send_keys(EMAIL).perform()
        time.sleep(1)
        ActionChains(driver).click(inputs[1]).send_keys(PASSWORD).perform()
        time.sleep(1)
        driver.find_element(By.CSS_SELECTOR, 'button[type="submit"]').click()
        time.sleep(8)
        print(f"[{datetime.now()}] Logged in successfully")

        driver.get('https://app.clockshark.com/App/Reports#CsvExport')
        time.sleep(5)

        driver.find_element(By.ID, 'date-picker').click()
        time.sleep(2)
        ranges = driver.find_elements(By.CSS_SELECTOR, '.ranges li')
        for r in ranges:
            if r.text.strip() == 'Last 14 Days':
                r.click()
                print(f"[{datetime.now()}] Selected Last 14 Days")
                break
        time.sleep(2)

        include_notes = driver.find_element(By.ID, 'includeNotes')
        if not include_notes.is_selected():
            include_notes.click()
        time.sleep(1)

        driver.find_element(By.XPATH, "//button[contains(text(),'Download CSV')]").click()
        print(f"[{datetime.now()}] Download clicked, waiting...")
        time.sleep(10)

        files = glob.glob(os.path.join(DOWNLOAD_DIR, '*.csv'))
        if files:
            latest = max(files, key=os.path.getctime)
            shutil.move(latest, FRESH_CSV)
            print(f"[{datetime.now()}] Fresh CSV saved to {FRESH_CSV}")
            return True
        else:
            print(f"[{datetime.now()}] ERROR: No CSV file found in {DOWNLOAD_DIR}")
            return False
    except Exception as e:
        print(f"[{datetime.now()}] Download error: {e}")
        return False
    finally:
        driver.quit()


# --- STEP 2: FILTER TO LAST 15 DAYS ---
def filter_to_15_days():
    print(f"[{datetime.now()}] Filtering to last {CUTOFF_DAYS} days...")
    cutoff = datetime.now() - timedelta(days=CUTOFF_DAYS)
    kept = 0
    skipped = 0
    rows = []

    with open(FRESH_CSV, 'r', encoding='utf-8-sig') as f:
        reader = csv.DictReader(f)
        for row in reader:
            try:
                start_dt = datetime.strptime(row['Start'].strip(), '%m/%d/%Y %I:%M:%S %p')
                if start_dt >= cutoff:
                    rows.append(row)
                    kept += 1
                else:
                    skipped += 1
            except:
                skipped += 1

    print(f"[{datetime.now()}] Kept {kept} rows, skipped {skipped} older rows")
    return rows


# --- STEP 3: PAR AUDIT ---
def clean_text(text):
    text = str(text)
    replacements = [
        ('\xe2\x80\x99', "'"), ('\xe2\x80\x9c', '"'), ('\xe2\x80\x9d', '"'),
        ('\xe2\x80\x93', '-'), ('\xe2\x80\x94', '-'),
        ('\xc3\xa9', 'e'), ('\xc3\xa8', 'e'),
        ('â\x80\x99', "'"), ('â\x80\x9c', '"'), ('â\x80\x9d', '"'),
        ('â\x80\x93', '-'), ('â\x80\x94', '-'),
    ]
    for bad, good in replacements:
        text = text.replace(bad, good)
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

Reply with ONLY two lines, exactly like this:
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


def run_par_audit(rows):
    print(f"[{datetime.now()}] Running PAR audit on {len(rows)} rows...")
    audited_rows = []
    critical = poor = fair = good = error = 0
    issues_found = 0

    for i, row in enumerate(rows):
        employee = (row.get('EmployeeFName', '') + ' ' + row.get('EmployeeLName', '')).strip()
        job = row.get('Customer: Job', '')
        task = row.get('Task', '')
        note = row.get('Notes', '')
        print(f"  [{i+1}/{len(rows)}] {employee} - {job[:35]}...")

        quality, recommendation = evaluate_note_with_ai(
            employee, job, task, row['Start'], row['End'], note
        )
        row['Audit_Quality'] = quality
        row['David_Recommendation'] = recommendation
        audited_rows.append(row)

        if quality == 'CRITICAL': critical += 1; issues_found += 1
        elif quality == 'POOR': poor += 1; issues_found += 1
        elif quality == 'FAIR': fair += 1; issues_found += 1
        elif quality == 'GOOD': good += 1
        elif quality == 'ERROR': error += 1

        time.sleep(0.3)

    # --- Write formatted Excel output ---
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "15-Day PAR Audit"

    thin = Side(style="thin", color="CCCCCC")
    border = Border(left=thin, right=thin, top=thin, bottom=thin)
    header_fill = PatternFill("solid", fgColor="1F3864")
    header_font = Font(bold=True, color="FFFFFF", size=11)
    header_align = Alignment(horizontal="center", vertical="center", wrap_text=True)

    quality_bg = {
        'GOOD':     'C6EFCE',
        'FAIR':     'FFEB9C',
        'POOR':     'FFAD00',
        'CRITICAL': 'FF0000',
        'ERROR':    'D3D3D3',
    }
    quality_fg = {
        'GOOD':     '006100',
        'FAIR':     '9C5700',
        'POOR':     '7B3F00',
        'CRITICAL': 'FFFFFF',
        'ERROR':    '000000',
    }
    row_highlight = {
        'CRITICAL': 'FFE0E0',
        'POOR':     'FFF3CD',
    }

    def sanitize(val):
        if val is None:
            return ''
        val = str(val)
        return ''.join(c for c in val if ord(c) >= 32 or c in '\t\n\r')

    for col_idx, col_name in enumerate(KEEP_COLUMNS, 1):
        cell = ws.cell(row=1, column=col_idx, value=col_name)
        cell.fill = header_fill
        cell.font = header_font
        cell.alignment = header_align
        cell.border = border

    for row_idx, row in enumerate(audited_rows, 2):
        quality = row.get('Audit_Quality', '')
        for col_idx, col_name in enumerate(KEEP_COLUMNS, 1):
            cell = ws.cell(row=row_idx, column=col_idx, value=sanitize(row.get(col_name, '')))
            cell.border = border
            cell.alignment = Alignment(vertical="top", wrap_text=True)
            if col_name == 'Audit_Quality' and quality in quality_bg:
                cell.fill = PatternFill("solid", fgColor=quality_bg[quality])
                cell.font = Font(bold=True, color=quality_fg[quality])
            elif quality in row_highlight:
                cell.fill = PatternFill("solid", fgColor=row_highlight[quality])

    col_widths = [20, 25, 12, 20, 10, 45, 14, 55]
    for i, width in enumerate(col_widths, 1):
        ws.column_dimensions[get_column_letter(i)].width = width

    ws.freeze_panes = "A2"
    ws.auto_filter.ref = f"A1:{get_column_letter(len(KEEP_COLUMNS))}1"
    for data_row in ws.iter_rows(min_row=2):
        ws.row_dimensions[data_row[0].row].height = 45

    wb.save(OUTPUT_XLSX)
    print(f"[{datetime.now()}] Excel saved to {OUTPUT_XLSX}")
    print(f"[{datetime.now()}] Audit complete. GOOD:{good} FAIR:{fair} POOR:{poor} CRITICAL:{critical} ERROR:{error}")
    return len(rows), issues_found, critical, poor, fair, good


# --- STEP 4: EMAIL RESULTS ---
def send_report(total, issues, critical, poor, fair, good):
    date_str = datetime.now().strftime("%m/%d/%Y")
    cutoff_str = (datetime.now() - timedelta(days=CUTOFF_DAYS)).strftime("%m/%d/%Y")

    body = (f"ManagerB, ManagerA, Owner,\n\n"
            f"Here is the 15-Day PAR Method Audit for shifts from {cutoff_str} through today ({date_str}).\n"
            f"This was run manually on demand.\n\n"
            f"Total Shifts Reviewed: {total}\n"
            f"Total Issues Found: {issues}\n\n"
            f"Breakdown:\n"
            f"  GOOD     (Meets standards):   {good}\n"
            f"  FAIR     (Could be improved): {fair}\n"
            f"  POOR     (Too vague/short):   {poor}\n"
            f"  CRITICAL (Blank/missing):     {critical}\n\n"
            f"Full audit report with David's recommendations is attached.\n\n"
            f"- David\nAcme Services Lead Auditor")

    cmd = [
        'gog', 'gmail', 'send',
        '--to', 'owner@example.com',
        '--subject', f'15-Day PAR Audit - {date_str}',
        '--body', body,
        '--account', 'bot@example.com',
        '--cc', 'manager1@example.com,manager2@example.com',
        '--attach', OUTPUT_XLSX
    ]

    print(f"[{datetime.now()}] Sending email...")
    result = subprocess.run(cmd, capture_output=True, text=True, env=env)
    if result.returncode == 0:
        print(f"[{datetime.now()}] Email sent successfully!")
    else:
        print(f"[{datetime.now()}] Email failed: {result.stderr}")


# --- MAIN ---
if __name__ == "__main__":
    print(f"\n{'='*60}")
    print(f"15-DAY PAR AUDIT - {datetime.now().strftime('%m/%d/%Y %I:%M %p')}")
    print(f"{'='*60}\n")

    success = download_fresh_csv()
    if not success:
        print("Download failed — aborting audit.")
        exit(1)

    rows = filter_to_15_days()
    if not rows:
        print("No rows in last 15 days — nothing to audit.")
        exit(0)

    total, issues, critical, poor, fair, good = run_par_audit(rows)
    send_report(total, issues, critical, poor, fair, good)

    print(f"\n[{datetime.now()}] 15-day PAR audit complete.\n")
