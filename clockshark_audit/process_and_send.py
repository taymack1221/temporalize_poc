import csv
import smtplib
import ssl
import os
from collections import defaultdict
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

# Configuration
SMTP_SERVER = "smtp.gmail.com"
SMTP_PORT = 587
SENDER_EMAIL = os.environ.get("SMTP_USER")
SENDER_PASS = os.environ.get("SMTP_PASS")
CC_EMAILS = ["owner@example.com", "manager1@example.com", "manager2@example.com"]
CSV_FILE = "/mnt/c/ClockSharkAudit/MISSING_NOTES_REPORT.csv"

def send_audit_emails():
    if not SENDER_EMAIL or not SENDER_PASS:
        print("Error: SMTP_USER or SMTP_PASS missing.")
        return

    # 1. Read and Group Data
    employees = defaultdict(list)
    try:
        with open(CSV_FILE, 'r', encoding='utf-8-sig') as f:
            reader = csv.DictReader(f)
            for row in reader:
                # Filter for missing notes (empty string or just whitespace)
                notes = row.get('Notes', '').strip()
                if not notes:
                    email = row.get('EmployeeEmail')
                    if email:
                        employees[email].append(row)
    except FileNotFoundError:
        print(f"Error: {CSV_FILE} not found.")
        return

    # 2. Connect to SMTP
    context = ssl.create_default_context()
    try:
        server = smtplib.SMTP(SMTP_SERVER, SMTP_PORT)
        server.starttls(context=context)
        server.login(SENDER_EMAIL, SENDER_PASS)
    except Exception as e:
        print(f"SMTP Connection Error: {e}")
        return

    # 3. Send Individual Emails
    print(f"Found {len(employees)} employees with missing notes.")
    
    for email_addr, rows in employees.items():
        first_name = rows[0].get('EmployeeFName', 'Employee')
        full_name = f"{rows[0].get('EmployeeFName')} {rows[0].get('EmployeeLName')}"
        
        # Build Body
        msg_body = f"Hi {first_name},\n\n"
        msg_body += "The following shifts are missing job notes. Please update them in ClockShark immediately:\n\n"
        
        for row in rows:
            date = row.get('Start', 'N/A').split(' ')[0]
            job = row.get('Customer: Job', 'N/A')
            hours = row.get('DecimalHours', '0')
            msg_body += f"- {date}: {job} ({hours} hrs)\n"
            
        msg_body += "\nThank you,\nAcme Services"

        # Construct Email
        msg = MIMEMultipart()
        msg['From'] = SENDER_EMAIL
        msg['To'] = email_addr
        msg['Cc'] = ", ".join(CC_EMAILS)
        msg['Subject'] = "Action Required: Missing ClockShark Notes"
        msg.attach(MIMEText(msg_body, 'plain'))
        
        recipients = [email_addr] + CC_EMAILS
        
        try:
            server.sendmail(SENDER_EMAIL, recipients, msg.as_string())
            print(f"✅ Sent to {full_name} ({email_addr}) - {len(rows)} items")
        except Exception as e:
            print(f"❌ Failed to send to {full_name}: {e}")

    server.quit()
    print("Mission Complete.")

if __name__ == "__main__":
    send_audit_emails()
