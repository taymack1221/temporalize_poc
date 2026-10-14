# -*- coding: utf-8 -*-
"""
weekly_par_trend.py
Runs every Friday at 3:00 PM.
1. Reads the last 5 days of PAR audit CSV archives
2. Calculates per-employee trend data
3. Identifies top performers, most improved, and who needs coaching
4. Emails ManagerA, ManagerB, and Owner with a formatted HTML report
"""
import csv
import os
import subprocess
import tempfile
from datetime import datetime, timedelta
from collections import defaultdict

# --- CONFIGURATION ---
ARCHIVE_DIR = '/mnt/c/ClockSharkAudit/archive'
DAYS_TO_REVIEW = 5

env = os.environ.copy()
env['GOG_KEYRING_PASSWORD'] = 'YOUR_GOG_KEYRING_PASSWORD'


# --- STEP 1: LOAD LAST N DAYS OF AUDIT DATA ---
def load_audit_history(days=DAYS_TO_REVIEW):
    print(f"[{datetime.now()}] Loading last {days} days of audit data...")
    all_rows = []
    files_loaded = []

    for i in range(days):
        date = (datetime.now() - timedelta(days=i)).strftime("%Y-%m-%d")
        filepath = os.path.join(ARCHIVE_DIR, f'PAR_AUDIT_{date}.csv')
        if os.path.exists(filepath):
            with open(filepath, 'r', encoding='utf-8') as f:
                reader = csv.DictReader(f)
                for row in reader:
                    row['_date'] = date
                    all_rows.append(row)
            files_loaded.append(date)
            print(f"  Loaded: PAR_AUDIT_{date}.csv")
        else:
            print(f"  Missing: PAR_AUDIT_{date}.csv")

    print(f"[{datetime.now()}] Loaded {len(all_rows)} rows from {len(files_loaded)} files")
    return all_rows, files_loaded


# --- STEP 2: CALCULATE TRENDS ---
def calculate_trends(all_rows):
    employee_data = defaultdict(lambda: {
        'GOOD': 0, 'FAIR': 0, 'POOR': 0, 'CRITICAL': 0, 'ERROR': 0, 'total': 0,
        'days': defaultdict(lambda: {'GOOD': 0, 'FAIR': 0, 'POOR': 0, 'CRITICAL': 0, 'total': 0})
    })

    for row in all_rows:
        emp = row.get('EmployeeID', '').strip()
        quality = row.get('Audit_Quality', '').strip().upper()
        date = row.get('_date', '')

        if not emp or quality == 'SKIPPED':
            continue

        employee_data[emp]['total'] += 1
        employee_data[emp]['days'][date]['total'] += 1

        if quality in ['GOOD', 'FAIR', 'POOR', 'CRITICAL', 'ERROR']:
            employee_data[emp][quality] += 1
            employee_data[emp]['days'][date][quality] = employee_data[emp]['days'][date].get(quality, 0) + 1

    results = {}
    for emp, data in employee_data.items():
        total = data['total']
        if total == 0:
            continue

        good_pct = round(data['GOOD'] / total * 100)
        poor_pct = round((data['POOR'] + data['CRITICAL']) / total * 100)
        fair_pct = round(data['FAIR'] / total * 100)
        critical_pct = round(data['CRITICAL'] / total * 100)

        dates = sorted(data['days'].keys())
        if len(dates) >= 2:
            mid = len(dates) // 2
            early_dates = dates[:mid]
            late_dates = dates[mid:]
            early_good = sum(data['days'][d].get('GOOD', 0) for d in early_dates)
            early_total = sum(data['days'][d]['total'] for d in early_dates)
            late_good = sum(data['days'][d].get('GOOD', 0) for d in late_dates)
            late_total = sum(data['days'][d]['total'] for d in late_dates)
            early_pct = round(early_good / early_total * 100) if early_total > 0 else 0
            late_pct = round(late_good / late_total * 100) if late_total > 0 else 0
            trend = late_pct - early_pct
        else:
            trend = 0

        results[emp] = {
            'total': total,
            'good_pct': good_pct,
            'fair_pct': fair_pct,
            'poor_pct': poor_pct,
            'critical_pct': critical_pct,
            'critical_count': data['CRITICAL'],
            'trend': trend,
            'raw': data
        }

    return results


# --- STEP 3: BUILD HTML REPORT ---
def build_html_report(results, files_loaded):
    if not results:
        return None, None

    date_str = datetime.now().strftime("%m/%d/%Y")
    week_start = (datetime.now() - timedelta(days=DAYS_TO_REVIEW)).strftime("%m/%d/%Y")

    all_totals = sum(r['total'] for r in results.values())
    all_good = sum(r['good_pct'] * r['total'] for r in results.values())
    team_good_pct = round(all_good / all_totals) if all_totals > 0 else 0

    consistent = [(e, r) for e, r in results.items() if r['good_pct'] >= 70]
    needs_coaching = [(e, r) for e, r in results.items() if r['poor_pct'] >= 50 or r['critical_count'] >= 3]
    most_improved = [(e, r) for e, r in results.items() if r['trend'] >= 20]
    declining = [(e, r) for e, r in results.items() if r['trend'] <= -20]

    consistent.sort(key=lambda x: x[1]['good_pct'], reverse=True)
    needs_coaching.sort(key=lambda x: x[1]['poor_pct'], reverse=True)
    most_improved.sort(key=lambda x: x[1]['trend'], reverse=True)
    all_sorted = sorted(results.items(), key=lambda x: x[1]['good_pct'], reverse=True)

    def quality_bar(good, fair, poor, critical):
        bar = ''
        if good > 0:
            bar += f'<span style="display:inline-block;width:{good}%;background:#00a651;height:14px;border-radius:3px 0 0 3px;" title="GOOD: {good}%"></span>'
        if fair > 0:
            bar += f'<span style="display:inline-block;width:{fair}%;background:#ffc107;height:14px;" title="FAIR: {fair}%"></span>'
        if poor > 0:
            bar += f'<span style="display:inline-block;width:{poor}%;background:#ff6b00;height:14px;" title="POOR: {poor}%"></span>'
        if critical > 0:
            bar += f'<span style="display:inline-block;width:{critical}%;background:#dc3545;height:14px;border-radius:0 3px 3px 0;" title="CRITICAL: {critical}%"></span>'
        return f'<div style="width:200px;background:#eee;border-radius:3px;display:inline-block;vertical-align:middle;">{bar}</div>'

    def trend_badge(trend):
        if trend >= 20:
            return '<span style="color:#00a651;font-weight:bold;">↑ Improving</span>'
        elif trend <= -20:
            return '<span style="color:#dc3545;font-weight:bold;">↓ Declining</span>'
        else:
            return '<span style="color:#888;">→ Steady</span>'

    def good_badge(pct):
        if pct >= 70:
            color = '#00a651'
        elif pct >= 40:
            color = '#ffc107'
        else:
            color = '#dc3545'
        return f'<span style="background:{color};color:#fff;padding:2px 8px;border-radius:12px;font-weight:bold;font-size:13px;">{pct}%</span>'

    html = f'''<!DOCTYPE html>
<html>
<head>
<meta charset="UTF-8">
<style>
  body {{ font-family: Arial, sans-serif; background: #f4f4f4; margin: 0; padding: 20px; }}
  .container {{ max-width: 800px; margin: 0 auto; background: #fff; border-radius: 10px; overflow: hidden; box-shadow: 0 2px 10px rgba(0,0,0,0.1); }}
  .header {{ background: #1a2c4e; color: #fff; padding: 30px; text-align: center; }}
  .header h1 {{ margin: 0; font-size: 26px; letter-spacing: 1px; }}
  .header p {{ margin: 8px 0 0; color: #c8a96e; font-size: 14px; }}
  .section {{ padding: 20px 30px; border-bottom: 1px solid #eee; }}
  .section h2 {{ font-size: 16px; margin: 0 0 15px; text-transform: uppercase; letter-spacing: 1px; }}
  .metric-box {{ display: inline-block; background: #1a2c4e; color: #fff; border-radius: 10px; padding: 15px 25px; text-align: center; margin: 5px; }}
  .metric-box .number {{ font-size: 36px; font-weight: bold; color: #c8a96e; }}
  .metric-box .label {{ font-size: 12px; opacity: 0.8; }}
  .employee-row {{ display: flex; align-items: center; padding: 10px 0; border-bottom: 1px solid #f0f0f0; gap: 15px; }}
  .employee-name {{ width: 160px; font-weight: bold; font-size: 14px; }}
  .pct-labels {{ font-size: 11px; color: #666; width: 220px; }}
  .coaching-item {{ background: #fff5f5; border-left: 4px solid #dc3545; padding: 10px 15px; margin: 8px 0; border-radius: 0 8px 8px 0; }}
  .good-item {{ background: #f0fff4; border-left: 4px solid #00a651; padding: 10px 15px; margin: 8px 0; border-radius: 0 8px 8px 0; }}
  .improved-item {{ background: #f0f8ff; border-left: 4px solid #007bff; padding: 10px 15px; margin: 8px 0; border-radius: 0 8px 8px 0; }}
  .footer {{ background: #f9f9f9; padding: 20px 30px; text-align: center; color: #888; font-size: 13px; }}
  .acme-gold {{ color: #c8a96e; font-weight: bold; }}
</style>
</head>
<body>
<div class="container">

  <div class="header">
    <h1>&#128202; Weekly PAR Audit Trend Report</h1>
    <p>Acme Services &nbsp;|&nbsp; {week_start} &ndash; {date_str} &nbsp;|&nbsp; Based on {len(files_loaded)} day(s) of data</p>
  </div>

  <div class="section" style="text-align:center;">
    <div class="metric-box">
      <div class="number">{team_good_pct}%</div>
      <div class="label">TEAM GOOD RATE</div>
    </div>
    <div class="metric-box">
      <div class="number">{len(results)}</div>
      <div class="label">EMPLOYEES REVIEWED</div>
    </div>
    <div class="metric-box">
      <div class="number">{all_totals}</div>
      <div class="label">TOTAL SHIFTS</div>
    </div>
  </div>
'''

    if most_improved:
        html += '''
  <div class="section">
    <h2 style="color:#007bff;">&#128200; Most Improved</h2>
'''
        for emp, r in most_improved[:3]:
            html += f'''
    <div class="improved-item">
      <strong>{emp}</strong> &mdash; GOOD rate up <strong>{r['trend']}%</strong> vs earlier this week
      &nbsp; {good_badge(r['good_pct'])} GOOD this week
    </div>
'''
        html += '  </div>\n'

    if consistent:
        html += '''
  <div class="section">
    <h2 style="color:#00a651;">&#9989; Consistent Performers</h2>
'''
        for emp, r in consistent[:5]:
            html += f'''
    <div class="good-item">
      <strong>{emp}</strong> &mdash; {good_badge(r['good_pct'])} GOOD &nbsp;
      <span style="color:#666;font-size:13px;">({r['total']} shifts reviewed)</span>
    </div>
'''
        html += '  </div>\n'

    if declining:
        html += '''
  <div class="section">
    <h2 style="color:#ff6b00;">&#128197; Declining This Week</h2>
'''
        for emp, r in declining[:3]:
            html += f'''
    <div class="coaching-item" style="border-color:#ff6b00;background:#fff8f0;">
      <strong>{emp}</strong> &mdash; GOOD rate down <strong>{abs(r['trend'])}%</strong> vs earlier this week
    </div>
'''
        html += '  </div>\n'

    if needs_coaching:
        html += '''
  <div class="section">
    <h2 style="color:#dc3545;">&#128308; Needs Coaching</h2>
'''
        for emp, r in needs_coaching[:5]:
            issues = []
            if r['critical_count'] >= 3:
                issues.append(f"{r['critical_count']} blank/critical notes")
            if r['poor_pct'] >= 50:
                issues.append(f"{r['poor_pct']}% POOR or worse")
            html += f'''
    <div class="coaching-item">
      <strong>{emp}</strong> &mdash; {', '.join(issues)}
    </div>
'''
        html += '  </div>\n'

    html += '''
  <div class="section">
    <h2 style="color:#333;">&#128203; Full Team Breakdown</h2>
    <table style="width:100%;border-collapse:collapse;font-size:13px;">
      <tr style="background:#1a2c4e;color:#fff;">
        <th style="padding:8px 10px;text-align:left;">Employee</th>
        <th style="padding:8px 10px;text-align:center;">GOOD</th>
        <th style="padding:8px 10px;text-align:center;">FAIR</th>
        <th style="padding:8px 10px;text-align:center;">POOR</th>
        <th style="padding:8px 10px;text-align:center;">CRITICAL</th>
        <th style="padding:8px 10px;text-align:left;">Quality Bar</th>
        <th style="padding:8px 10px;text-align:center;">Trend</th>
        <th style="padding:8px 10px;text-align:center;">Shifts</th>
      </tr>
'''
    for i, (emp, r) in enumerate(all_sorted):
        bg = '#fafafa' if i % 2 == 0 else '#fff'
        html += f'''
      <tr style="background:{bg};">
        <td style="padding:8px 10px;font-weight:bold;">{emp}</td>
        <td style="padding:8px 10px;text-align:center;color:#00a651;font-weight:bold;">{r['good_pct']}%</td>
        <td style="padding:8px 10px;text-align:center;color:#856404;">{r['fair_pct']}%</td>
        <td style="padding:8px 10px;text-align:center;color:#ff6b00;">{r['poor_pct']}%</td>
        <td style="padding:8px 10px;text-align:center;color:#dc3545;font-weight:bold;">{r['critical_count']}</td>
        <td style="padding:8px 10px;">{quality_bar(r['good_pct'], r['fair_pct'], r['poor_pct'] - r['critical_pct'], r['critical_pct'])}</td>
        <td style="padding:8px 10px;text-align:center;">{trend_badge(r['trend'])}</td>
        <td style="padding:8px 10px;text-align:center;color:#666;">{r['total']}</td>
      </tr>
'''

    html += f'''
    </table>
  </div>

  <div class="footer">
    <p><span class="acme-gold">&#128736; David</span> &mdash; Acme Services Lead Auditor</p>
    <p style="font-size:11px;">Generated {datetime.now().strftime("%m/%d/%Y %I:%M %p")} &nbsp;|&nbsp; Acme Services, Inc.</p>
  </div>

</div>
</body>
</html>'''

    subject = f"Weekly PAR Trend Report - {date_str}"
    return subject, html


# --- STEP 4: EMAIL HTML REPORT ---
def send_report(subject, html_body):
    # Save HTML to temp file and send via gog
    tmp = tempfile.NamedTemporaryFile(mode='w', suffix='.html', delete=False, encoding='utf-8')
    tmp.write(html_body)
    tmp.close()

    # Save HTML file for reference
    html_path = f'/mnt/c/ClockSharkAudit/weekly_trend_{datetime.now().strftime("%Y-%m-%d")}.html'
    with open(html_path, 'w', encoding='utf-8') as f:
        f.write(html_body)
    print(f"[{datetime.now()}] HTML saved to {html_path}")

    cmd = [
        'gog', 'gmail', 'send',
        '--to', 'manager1@example.com',
        '--subject', subject,
        '--body-html', html_body,
        '--account', 'bot@example.com',
        '--cc', 'manager2@example.com,owner@example.com'
    ]

    print(f"[{datetime.now()}] Sending weekly trend report...")
    result = subprocess.run(cmd, capture_output=True, text=True, env=env)
    if result.returncode == 0:
        print(f"[{datetime.now()}] Email sent successfully!")
    else:
        print(f"[{datetime.now()}] Email failed: {result.stderr}")
        # Fallback — try without --html flag
        print(f"[{datetime.now()}] Trying plain text fallback...")
        plain = f"Weekly PAR Trend Report - {datetime.now().strftime('%m/%d/%Y')}\n\nSee attached HTML report at {html_path}"
        cmd2 = [
            'gog', 'gmail', 'send',
            '--to', 'manager1@example.com',
            '--subject', subject,
            '--body', plain,
            '--account', 'bot@example.com',
            '--cc', 'manager2@example.com,owner@example.com'
        ]
        result2 = subprocess.run(cmd2, capture_output=True, text=True, env=env)
        if result2.returncode == 0:
            print(f"[{datetime.now()}] Fallback email sent!")
        else:
            print(f"[{datetime.now()}] Fallback also failed: {result2.stderr}")

    os.unlink(tmp.name)


# --- MAIN ---
if __name__ == "__main__":
    print(f"\n{'='*60}")
    print(f"WEEKLY PAR TREND REPORT - {datetime.now().strftime('%m/%d/%Y %I:%M %p')}")
    print(f"{'='*60}\n")

    all_rows, files_loaded = load_audit_history()

    if not all_rows:
        print("No audit data found in archive. Nothing to report.")
        exit(0)

    results = calculate_trends(all_rows)

    if not results:
        print("No employee data found. Nothing to report.")
        exit(0)

    subject, html_body = build_html_report(results, files_loaded)

    if not subject:
        print("Could not build report.")
        exit(0)

    print(f"[{datetime.now()}] Report built — {len(results)} employees, sending email...")
    send_report(subject, html_body)

    print(f"\n[{datetime.now()}] Weekly PAR trend report complete.\n")
