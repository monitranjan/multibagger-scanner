"""
Chartink Premium Backtest Data Downloader Helper
────────────────────────────────────────────────
This script helps you automate downloading your 9-month backtest data
directly from your premium Chartink screener using your active browser session.

How to use:
1. Open your browser, log in to Chartink, and go to: https://chartink.com/screener/monit-momentum
2. Open Developer Tools (F12) -> Network tab -> click "CSV" download on the Backtest Results table.
3. Find the network request for the CSV download, copy the headers (ci_session cookie and X-CSRF-TOKEN).
4. Paste them into the CONFIG section below or in a .env file.
5. Run: python3 download_backtest.py
"""

import os
import sys
import requests
import pandas as pd
from bs4 import BeautifulSoup

# ─── CONFIGURATION (PASTE YOUR BROWSER HEADERS HERE) ──────────────────────────
# Copy these from your active browser session when logged in
CI_SESSION_COOKIE = ""  # e.g. "eyJpdiI6Ik93R..."
X_CSRF_TOKEN = ""       # e.g. "m14cjy9eXRy2OVTkg..."
SCANNER_SLUG = "monit-momentum"

def download_backtest_data():
    if not CI_SESSION_COOKIE or not X_CSRF_TOKEN:
        print("⚠️  Error: Please configure CI_SESSION_COOKIE and X_CSRF_TOKEN inside this script first!")
        print("Follow the instructions in the docstring to copy them from your active browser session.")
        sys.exit(1)

    print(f"🔄 Connecting to Chartink screener: {SCANNER_SLUG}...")
    
    session = requests.Session()
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "X-CSRF-TOKEN": X_CSRF_TOKEN,
        "Referer": f"https://chartink.com/screener/{SCANNER_SLUG}"
    })
    
    # Set the session cookie
    cookies = {
        "ci_session": CI_SESSION_COOKIE
    }
    
    # 1. Fetch the screener page first to establish cookies
    url = f"https://chartink.com/screener/{SCANNER_SLUG}"
    try:
        r = session.get(url, cookies=cookies, timeout=20)
        if r.status_code != 200:
            print(f"❌ Failed to access screener page. Status code: {r.status_code}")
            return
    except Exception as e:
        print(f"❌ Error accessing screener page: {e}")
        return

    # 2. Extract the scan_clause from the HTML
    soup = BeautifulSoup(r.text, "html.parser")
    scanner_tag = soup.find("scanner")
    if not scanner_tag:
        print("❌ Could not find the <scanner> tag. Make sure you are logged in and the screener is public/accessible.")
        return
        
    import html
    import json
    scan_json_str = html.unescape(scanner_tag.get(":scan-json"))
    scan_data = json.loads(scan_json_str)
    
    scan_clause = scan_data.get("atlas_query")
    if not scan_clause:
        print("❌ Could not extract the scan_clause/atlas_query from page configuration.")
        return
        
    print("✅ Successfully retrieved scan clause from Chartink!")
    
    # 3. Post to the Backtest download endpoint
    # Note: On Chartink premium, clicking CSV triggers a download request
    # usually post to /screener/process with download parameters or similar endpoint.
    download_url = "https://chartink.com/screener/process"
    payload = {
        "scan_clause": scan_clause,
        "download_excel": "1", # Parameter for CSV/Excel download
        "backtest": "1"        # Trigger historical backtest data instead of live
    }
    
    print("🔄 Requesting historical 9-month backtest data (sending POST request)...")
    try:
        resp = session.post(download_url, data=payload, cookies=cookies, timeout=30)
        if resp.status_code == 200:
            output_file = "Backtest Monit momentum (2).csv"
            with open(output_file, "wb") as f:
                f.write(resp.content)
            print(f"🎉 SUCCESS! Saved fresh backtest data to: {output_file} ({len(resp.content)} bytes)")
        else:
            print(f"❌ Failed to download backtest. Status code: {resp.status_code}")
            print(resp.text[:500])
    except Exception as e:
        print(f"❌ Error downloading backtest data: {e}")

if __name__ == "__main__":
    # If variables are set in environment, use them
    CI_SESSION_COOKIE = os.environ.get("CI_SESSION_COOKIE", CI_SESSION_COOKIE)
    X_CSRF_TOKEN = os.environ.get("X_CSRF_TOKEN", X_CSRF_TOKEN)
    download_backtest_data()
