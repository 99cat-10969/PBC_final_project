"""
台北市運動中心人數監控
GitHub Actions 版本：每次執行一次，結果寫入 Google Sheets
時段限制（06:00-22:00 台北時間）由 Actions cron 控制
"""
import os
import re
import time
from datetime import datetime
from typing import Optional
from zoneinfo import ZoneInfo

from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
import gspread
from google.oauth2.service_account import Credentials
import json

PAGE_URL  = "https://booking-tpsc.sporetrofit.com/Home/LocationPeopleNum"
XINYI_URL = "https://xysc.teamxports.com/"
TZ        = ZoneInfo("Asia/Taipei")

# Google Sheets 設定（從環境變數讀取，在 GitHub Secrets 設定）
SPREADSHEET_ID = os.environ["SPREADSHEET_ID"]       # Google Sheet 的 ID
GSHEET_CREDS   = os.environ["GSHEET_CREDENTIALS"]   # Service Account JSON 字串


# ── Selenium ────────────────────────────────────────────

def make_driver(stealth=False):
    opts = Options()
    opts.add_argument("--headless=new")
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")
    opts.add_argument("--disable-gpu")
    opts.add_argument("--log-level=3")
    if stealth:
        opts.add_argument("--disable-blink-features=AutomationControlled")
        opts.add_experimental_option("excludeSwitches", ["enable-automation"])
        opts.add_experimental_option("useAutomationExtension", False)
        opts.add_argument(
            "--user-agent=Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        )
    return webdriver.Chrome(options=opts)


# ── 爬蟲：其他場館 ───────────────────────────────────────

def parse_centers(body_text: str) -> list:
    results = []
    lines = [l.strip() for l in body_text.splitlines() if l.strip()]
    i = 0
    while i < len(lines):
        line = lines[i]
        if "運動中心" in line and "信義" not in line and i + 1 < len(lines):
            center_name = line
            data_line   = lines[i + 1]
            parts = data_line.split("人")
            if len(parts) >= 3 and parts[0].isdigit():
                pool   = int(parts[0])
                middle = parts[1]
                gym    = None
                for cut in range(1, len(middle)):
                    cap_part = int(middle[:cut])
                    gym_part = int(middle[cut:]) if middle[cut:].isdigit() else None
                    if gym_part is not None and cap_part >= gym_part:
                        gym = gym_part
                        break
                if gym is not None:
                    results.append({"場館": center_name, "游泳池": pool, "健身房": gym})
            i += 2
        else:
            i += 1
    return results


def fetch_all_centers() -> Optional[list]:
    driver = None
    try:
        driver = make_driver()
        driver.get(PAGE_URL)
        time.sleep(6)
        body_text = driver.find_element(By.TAG_NAME, "body").text
        return parse_centers(body_text)
    except Exception as e:
        print(f"[!] 其他場館抓取失敗：{e}")
        return None
    finally:
        if driver:
            driver.quit()


# ── 爬蟲：信義運動中心 ────────────────────────────────────

def fetch_xinyi() -> dict:
    driver = None
    try:
        driver = make_driver(stealth=True)
        driver.get(XINYI_URL)
        time.sleep(8)
        els  = driver.find_elements(By.CSS_SELECTOR, "p.text-blue-700, p[class*='text-blue-700']")
        nums = [int(el.text.strip()) for el in els if el.text.strip().isdigit()]
        if len(nums) >= 2:
            return {"場館": "信義運動中心", "游泳池": nums[0], "健身房": nums[1]}
        print(f"[!] 信義：只找到 {len(nums)} 個數字")
        return {"場館": "信義運動中心", "游泳池": "ERROR", "健身房": "ERROR"}
    except Exception as e:
        print(f"[!] 信義抓取失敗：{e}")
        return {"場館": "信義運動中心", "游泳池": "ERROR", "健身房": "ERROR"}
    finally:
        if driver:
            driver.quit()


# ── Google Sheets ────────────────────────────────────────

def get_sheet():
    creds_dict = json.loads(GSHEET_CREDS)
    creds = Credentials.from_service_account_info(
        creds_dict,
        scopes=["https://www.googleapis.com/auth/spreadsheets"],
    )
    client = gspread.authorize(creds)
    sh     = client.open_by_key(SPREADSHEET_ID)

    # 若工作表不存在則建立
    try:
        ws = sh.worksheet("人數紀錄")
    except gspread.WorksheetNotFound:
        ws = sh.add_worksheet(title="人數紀錄", rows=10000, cols=4)
        ws.append_row(["時間", "場館", "游泳池人數", "健身房人數"])
    return ws


def save_to_gsheets(rows: list, timestamp: str):
    ws = get_sheet()
    batch = [[timestamp, r["場館"], r["游泳池"], r["健身房"]] for r in rows]
    ws.append_rows(batch, value_input_option="USER_ENTERED")
    print(f"  → 已寫入 Google Sheets（{len(batch)} 筆）")


# ── 主程式 ────────────────────────────────────────────────

def main():
    now = datetime.now(TZ)
    print(f"執行時間（台北）：{now.strftime('%Y-%m-%d %H:%M:%S')}")

    centers = fetch_all_centers()
    if not centers:
        print("所有場館抓取失敗，結束")
        return

    xinyi = fetch_xinyi()
    centers.append(xinyi)

    timestamp = now.strftime("%Y-%m-%d %H:%M:%S")
    for c in centers:
        print(f"  {c['場館']:<10} 游泳池:{c['游泳池']}  健身房:{c['健身房']}")

    save_to_gsheets(centers, timestamp)


if __name__ == "__main__":
    main()
