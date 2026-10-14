import csv
import subprocess
import os
import random
import urllib.request
import json

env = os.environ.copy()
env['GOG_KEYRING_PASSWORD'] = 'YOUR_GOG_KEYRING_PASSWORD'

GEMINI_API_KEY = 'YOUR_GEMINI_API_KEY'

def get_joke():
    try:
        url = 'https://generativelanguage.googleapis.com/v1beta/models/gemini-flash-latest:generateContent?key=' + GEMINI_API_KEY
        payload = json.dumps({"contents":[{"parts":[{"text":"Tell me one short clean work-appropriate joke. Just the joke no intro no explanation."}]}]}).encode('utf-8')
        req = urllib.request.Request(url, data=payload, headers={'Content-Type': 'application/json'})
        with urllib.request.urlopen(req, timeout=10) as response:
            data = json.loads(response.read())
            return data['candidates'][0]['content']['parts'][0]['text'].strip()
    except Exception as e:
        fallback = [
            "Why don\'t scientists trust atoms? Because they make up everything!",
            "What do you call a fake noodle? An impasta!",
            "Why did the scarecrow win an award? Because he was outstanding in his field!",
            "I told my wife she was drawing her eyebrows too high. She looked surprised.",
            "Why don\'t skeletons fight each other? They don\'t have the guts."
        ]
        return random.choice(fallback)

def send_emails():
    with open('/mnt/c/ClockSharkAudit/MISSING_NOTES_REPORT.csv', encoding='utf-8-sig') as f:
        reader = csv.DictReader(f)
        missing = {}
        for row in reader:
            if not row['Notes'].strip():
                emp = row['EmployeeEmail']
                name = row['EmployeeFName'] + ' ' + row['EmployeeLName']
                if emp not in missing:
                    missing[emp] = {'name': name, 'shifts': []}
                missing[emp]['shifts'].append(row)

    for email, data in missing.items():
        name = data['name']
        shifts = data['shifts']
        shift_list = ""
        for s in shifts:
            shift_list += "  - " + s['Customer: Job'] + " | " + s['Start'] + " to " + s['End'] + "\n"

        joke = get_joke()

        body = "Hi " + name.split()[0] + ",\n\nThis is a reminder that the following shifts are missing notes in ClockShark:\n\n" + shift_list + "\nPlease log into ClockShark and add notes for each shift listed above as soon as possible.\n\nHere's a little something to brighten your day:\n" + joke + "\n\nBest regards,\nDavid - Acme Services Audit"

        cmd = [
            'gog', 'gmail', 'send',
            '--to', email,
            '--subject', 'Missing Shift Notes Required',
            '--body', body,
            '--account', 'bot@example.com',
            '--cc', 'owner@example.com'
        ]
        print("Sending to " + name + " (" + email + ") - " + str(len(shifts)) + " shifts...")
        result = subprocess.run(cmd, capture_output=True, text=True, env=env)
        if result.returncode == 0:
            print("  Sent")
        else:
            print("  Failed: " + result.stderr)

send_emails()
