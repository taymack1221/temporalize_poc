import time
import os
import glob
import shutil
from datetime import datetime
from dotenv import load_dotenv
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.common.action_chains import ActionChains
load_dotenv('/mnt/c/ClockSharkAudit/.env')
EMAIL = os.getenv('CLOCKSHARK_EMAIL')
PASSWORD = os.getenv('CLOCKSHARK_PASSWORD')
DOWNLOAD_DIR = '/mnt/c/ClockSharkAudit/downloads'
OUTPUT_FILE = '/mnt/c/ClockSharkAudit/MONTH_FRESH.csv'
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
print(f"[{datetime.now()}] Starting ClockShark month download...")
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
    print(f"[{datetime.now()}] On CSV Export page")
    driver.find_element(By.ID, 'date-picker').click()
    time.sleep(2)
    ranges = driver.find_elements(By.CSS_SELECTOR, '.ranges li')
    for r in ranges:
        if r.text.strip() == 'This Month':
            r.click()
            print(f"[{datetime.now()}] Selected This Month")
            break
    time.sleep(2)
    include_notes = driver.find_element(By.ID, 'includeNotes')
    if not include_notes.is_selected():
        include_notes.click()
    time.sleep(1)
    driver.find_element(By.XPATH, "//button[contains(text(),'Download CSV')]").click()
    print(f"[{datetime.now()}] Download CSV clicked, waiting...")
    time.sleep(10)
    files = glob.glob(os.path.join(DOWNLOAD_DIR, '*.csv'))
    if files:
        latest = max(files, key=os.path.getctime)
        shutil.move(latest, OUTPUT_FILE)
        print(f"[{datetime.now()}] File saved to {OUTPUT_FILE}")
    else:
        print(f"[{datetime.now()}] ERROR: No CSV file found in {DOWNLOAD_DIR}")
finally:
    driver.quit()
print(f"[{datetime.now()}] Done!")
