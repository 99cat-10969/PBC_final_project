"""
台北市運動中心人數監控
GitHub Actions 版本：每次執行一次，結果寫入 Google Sheets
不使用 Selenium，改用 requests 直接打 API
"""
import os
import re
import json
import time
from datetime import datetime
from typing import Optional
from zoneinfo import ZoneInfo
 
import requests
import gspread
from google.oauth2.service_account import Credentials
 
TZ = ZoneInfo("Asia/Taipei")
 
TPSC_PAGE = "https://booking-tpsc.sporetrofit.com/Home/LocationPeopleNum"
TPSC_API  = "https://booking-tpsc.sporetrofit.com/Home/loadLocationPeopleNum"
XINYI_PAGE = "https://xysc.teamxports.com/faq"
XINYI_API  = "https://xysc.teamxports.com/get-court-cat-people-flow"
 
SPREADSHEET_ID = os.environ["SPREADSHEET_ID"]
GSHEET_CREDS   = os.environ["GSHEET_CREDENTIALS"]
 
HEADERS_BASE = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36",
    "Accept-Language": "zh-TW,zh;q=0.9,en-US;q=0.8,en;q=0.7",
}
 
 
# ── 其他場館（POST API）────────────────────────────────────
 
def parse_tpsc_json(data) -> list:
    """
    解析 loadLocationPeopleNum 的 JSON 回應
    先印出原始結構，再依格式提取
    """
    results = []
    # JSON 格式：{"locationPeopleNums": [{LID, lidName, swPeopleNum, gymPeopleNum, ...}]}
    items = []
    if isinstance(data, dict):
        items = data.get("locationPeopleNums", [])
    elif isinstance(data, list):
        items = data
 
    for item in items:
        name = item.get("lidName", "")
        pool = item.get("swPeopleNum", 0)
        gym  = item.get("gymPeopleNum", 0)
        if name and "信義" not in name:
            results.append({
                "場館": name + "運動中心",
                "游泳池": int(pool),
                "健身房": int(gym),
            })
 
    return results
 
 
def fetch_all_centers() -> Optional[list]:
    session = requests.Session()
    session.headers.update(HEADERS_BASE)
 
    try:
        # Step 1：先訪問頁面取得 session cookie
        print("  → 取得 TPSC session...")
        resp = session.get(TPSC_PAGE, timeout=15)
        resp.raise_for_status()
        print(f"  → Session cookies: {dict(session.cookies)}")
 
        # Step 2：POST 打 API
        print("  → 呼叫 loadLocationPeopleNum API...")
        api_resp = session.post(
            TPSC_API,
            headers={
                "X-Requested-With": "XMLHttpRequest",
                "Referer": TPSC_PAGE,
                "Origin": "https://booking-tpsc.sporetrofit.com",
                "Content-Length": "0",
            },
            timeout=15,
        )
        api_resp.raise_for_status()
        print(f"  → Status: {api_resp.status_code}, Content-Type: {api_resp.headers.get('Content-Type')}")
        print(f"  → Raw response: {api_resp.text[:500]}")
 
        data = api_resp.json()
        results = parse_tpsc_json(data)
 
        if not results:
            print("  [!] JSON 解析出 0 筆，嘗試用文字解析...")
            results = parse_text_fallback(api_resp.text)
 
        return results if results else None
 
    except Exception as e:
        print(f"  [!] TPSC 抓取失敗：{e}")
        return None
 
 
def parse_text_fallback(text: str) -> list:
    """備援：若 JSON 結構不如預期，嘗試從文字中提取"""
    results = []
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    i = 0
    while i < len(lines):
        line = lines[i]
        if "運動中心" in line and "信義" not in line and i + 1 < len(lines):
            center_name = line
            data_line = lines[i + 1]
            parts = data_line.split("人")
            if len(parts) >= 3 and parts[0].isdigit():
                pool = int(parts[0])
                middle = parts[1]
                gym = None
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
 
 
# ── 信義運動中心 ──────────────────────────────────────────
 
def fetch_xinyi() -> dict:
    """
    先訪問 faq 頁面取得 session/cookie，
    再打 get-court-cat-people-flow API
    siteId=3 游泳池，siteId=4 健身房
    """
    session = requests.Session()
    session.headers.update(HEADERS_BASE)
 
    try:
        print("  → 取得信義 session...")
        session.get(XINYI_PAGE, timeout=15)
 
        pool, gym = None, None
        for site_id, label in [(3, "游泳池"), (4, "健身房")]:
            resp = session.get(
                XINYI_API,
                params={"siteId": site_id},
                headers={"Referer": XINYI_PAGE},
                timeout=10,
            )
            print(f"  [信義 siteId={site_id}] status={resp.status_code} body={resp.text[:200]}")
            if resp.status_code == 200 and resp.text.strip():
                data = resp.json()
                count = (data.get("currentPeople") or data.get("count") or
                         data.get("people") or data.get("num") or
                         data.get("CurrentNum") or 0)
                if site_id == 3:
                    pool = count
                else:
                    gym = count
 
        return {
            "場館": "信義運動中心",
            "游泳池": pool if pool is not None else "ERROR",
            "健身房": gym  if gym  is not None else "ERROR",
        }
 
    except Exception as e:
        print(f"  [!] 信義抓取失敗：{e}")
        return {"場館": "信義運動中心", "游泳池": "ERROR", "健身房": "ERROR"}
 
 
# ── Google Sheets ─────────────────────────────────────────
 
def get_sheet():
    creds = Credentials.from_service_account_info(
        json.loads(GSHEET_CREDS),
        scopes=["https://www.googleapis.com/auth/spreadsheets"],
    )
    client = gspread.authorize(creds)
    sh = client.open_by_key(SPREADSHEET_ID)
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
    print("\n結果：")
    for c in centers:
        print(f"  {c['場館']:<10} 游泳池:{c['游泳池']}  健身房:{c['健身房']}")
 
    save_to_gsheets(centers, timestamp)
 
 
if __name__ == "__main__":
    main()
 
