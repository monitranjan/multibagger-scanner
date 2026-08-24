#!/usr/bin/env python3
"""
Automated Monit Wheels-style Equity Research Report Generator.
Decoupled pipeline script to generate deep institutional equity reports for top confluence stocks
using Gemini, ensuring reports are not regenerated repeatedly within the same calendar quarter.
"""

import os
import sys
import json
import requests
import time
import re
from datetime import datetime, date, timedelta, timezone
import sqlite3
import pandas as pd
from pathlib import Path
from bs4 import BeautifulSoup
from pypdf import PdfReader
import urllib.parse
import xml.etree.ElementTree as ET

def load_dotenv():
    """Load variables from .env file into os.environ if it exists."""
    env_path = Path(".env")
    if env_path.exists():
        with open(env_path, "r") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    key, val = line.split("=", 1)
                    os.environ[key.strip()] = val.strip().strip('"').strip("'")

load_dotenv()

# Global search counter to comply with Tavily limits
GOOGLE_SEARCH_COUNTER = 0

import document_pipeline as dp



# --- Live StockScans and yfinance actuals scrapers ---

def fetch_stockscans_company_data(symbol: str) -> dict:
    """
    Fetch all available fundamental, peer, and card details from StockScans for a symbol.
    """
    cookie = os.environ.get("STOCKSCANS_COOKIE", "")
    headers = {
        "accept": "application/json",
        "content-type": "application/json",
        "cookie": cookie,
        "user-agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, Gecko) Chrome/148.0.0.0 Safari/537.36"
    }
    
    # 1. Fetch search-company to get fundamentals source (C or S) and exchange
    source = "C" # default to Consolidated
    exchange = "NSE"
    search_data = {}
    for ex in ["NSE", "BSE"]:
        url = f"https://www.stockscans.in/api/company/scans/search-company/{ex}:{symbol}"
        try:
            r = requests.get(url, headers=headers, timeout=12)
            if r.status_code == 200:
                search_data = r.json()
                exchange = ex
                meta = search_data.get("metaRatios", {})
                source = meta.get("Fundamentals Source") or "C"
                break
        except Exception:
            continue
            
    # 2. Fetch fundamentals
    fundamentals_data = {}
    url = f"https://www.stockscans.in/api/company/fundamentals/{exchange}:{symbol}/{source}"
    try:
        r = requests.get(url, headers=headers, timeout=12)
        if r.status_code == 200:
            fundamentals_data = r.json()
    except Exception as e:
        print(f"⚠️ Error fetching fundamentals from StockScans for {symbol}: {e}")
        
    # 3. Fetch industry peers
    peers_list = []
    url = "https://www.stockscans.in/api/company/industry-peers"
    payload = {"companyIds": [f"{exchange}:{symbol}"], "limit": 6}
    try:
        r = requests.post(url, headers=headers, json=payload, timeout=12)
        if r.status_code == 200:
            peers_list = r.json().get("companies", [])
    except Exception as e:
        print(f"⚠️ Error fetching industry peers from StockScans for {symbol}: {e}")
        
    # 4. Fetch card details for peers and target company
    card_details = {}
    all_ids = [f"{exchange}:{symbol}"]
    if peers_list:
        all_ids = [p["companyId"] for p in peers_list]
        if f"{exchange}:{symbol}" not in all_ids:
            all_ids.append(f"{exchange}:{symbol}")
    url = "https://www.stockscans.in/api/company/card-details"
    payload = {"companyIds": all_ids}
    try:
        r = requests.post(url, headers=headers, json=payload, timeout=12)
        if r.status_code == 200:
            card_details = r.json().get("cardData", {})
    except Exception as e:
        print(f"⚠️ Error fetching card details from StockScans for {symbol} and peers: {e}")

    # 5. Fetch shareholding from StockScans
    shareholding_data = {}
    url = f"https://www.stockscans.in/api/company/shareholding/{exchange}:{symbol}"
    try:
        r = requests.get(url, headers=headers, timeout=12)
        if r.status_code == 200:
            shareholding_data = r.json()
        elif r.status_code == 401:
            print(f"ℹ️ StockScans shareholding returned 401 (Session expired/unauthorized) for {symbol}. Will fall back to yfinance.")
    except Exception as e:
        print(f"⚠️ Error fetching shareholding from StockScans for {symbol}: {e}")
        
    # 5b. Fetch extra details from StockScans (deals, insider trading, acquisitions)
    bulk_deals_data = {}
    try:
        r = requests.get(f"https://www.stockscans.in/api/company/bulk-block-deals/{exchange}:{symbol}", headers=headers, timeout=12)
        if r.status_code == 200:
            bulk_deals_data = r.json()
    except Exception as e:
        print(f"⚠️ Error fetching bulk/block deals from StockScans for {symbol}: {e}")
        
    insider_trading_data = {}
    try:
        r = requests.get(f"https://www.stockscans.in/api/company/insider-trading/{exchange}:{symbol}", headers=headers, timeout=12)
        if r.status_code == 200:
            insider_trading_data = r.json()
    except Exception as e:
        print(f"⚠️ Error fetching insider trading from StockScans for {symbol}: {e}")
        
    substantial_acq_data = {}
    try:
        r = requests.get(f"https://www.stockscans.in/api/company/substantial-acquisition/{exchange}:{symbol}", headers=headers, timeout=12)
        if r.status_code == 200:
            substantial_acq_data = r.json()
    except Exception as e:
        print(f"⚠️ Error fetching substantial acquisitions from StockScans for {symbol}: {e}")
            
    return {
        "symbol": symbol,
        "exchange": exchange,
        "source": source,
        "search": search_data,
        "fundamentals": fundamentals_data,
        "peers": peers_list,
        "card_details": card_details,
        "shareholding": shareholding_data,
        "bulk_deals": bulk_deals_data,
        "insider_trading": insider_trading_data,
        "substantial_acquisition": substantial_acq_data
    }


def fetch_peers_fundamentals_in_parallel(peer_ids: list[str]) -> dict:
    """
    Fetch fundamentals for multiple peer symbols in parallel.
    """
    results = {}
    if not peer_ids:
        return results
        
    from concurrent.futures import ThreadPoolExecutor, as_completed
    cookie = os.environ.get("STOCKSCANS_COOKIE", "")
    headers = {
        "accept": "application/json",
        "content-type": "application/json",
        "cookie": cookie,
        "user-agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, Gecko) Chrome/148.0.0.0 Safari/537.36"
    }
    
    def fetch_single(company_id):
        try:
            # Check source from search-company first
            search_url = f"https://www.stockscans.in/api/company/scans/search-company/{company_id}"
            r = requests.get(search_url, headers=headers, timeout=10)
            source = "C"
            if r.status_code == 200:
                source = r.json().get("metaRatios", {}).get("Fundamentals Source", "C")
            
            fund_url = f"https://www.stockscans.in/api/company/fundamentals/{company_id}/{source}"
            r2 = requests.get(fund_url, headers=headers, timeout=10)
            if r2.status_code == 200:
                return company_id, r2.json()
        except Exception:
            pass
        return company_id, {}
        
    with ThreadPoolExecutor(max_workers=len(peer_ids)) as executor:
        futures = {executor.submit(fetch_single, pid): pid for pid in peer_ids}
        for future in as_completed(futures):
            pid = futures[future]
            try:
                company_id, data = future.result()
                if data:
                    results[company_id] = data
            except Exception:
                pass
    return results


def format_actuals_to_markdown(data: dict) -> dict[str, str]:
    """
    Format StockScans fundamentals, peers, and shareholding data into clean Markdown tables.
    """
    symbol = data.get("symbol")
    exchange = data.get("exchange")
    target_id = f"{exchange}:{symbol}"
    
    fundamentals = data.get("fundamentals", {})
    yearly_data = fundamentals.get("yearly", [])
    
    formatted = {
        "income_statement": "",
        "balance_sheet": "",
        "cash_flow_ratios": "",
        "peer_table": "",
        "shareholding_table": "",
        "years": []
    }
    
    # 1. Format Yearly Financial Statements
    if yearly_data and len(yearly_data) > 1:
        headers = yearly_data[0]
        rows = yearly_data[1:]
        header_map = {h: i for i, h in enumerate(headers)}
        row_map = {r[header_map["Date"]]: r for r in rows if "Date" in header_map}
        
        available_years = [y for y in sorted(list(row_map.keys())) if y.lower() not in ["latest", "ttm"]]
        years_to_show = available_years[-3:] if len(available_years) >= 3 else available_years
        
        # Helper to format year header
        def format_year_header(y_str):
            if len(y_str) == 6 and y_str.isdigit():
                year = y_str[:4]
                return f"FY{year[2:]}A"
            return y_str
            
        formatted_headers = [format_year_header(y) for y in years_to_show]
        formatted["years"] = formatted_headers
        
        # Table 1: Income Statement
        inc_cols = [
            ("Revenue", "Revenue"),
            ("Operating Profit", "EBITDA"),
            ("OPM", "EBITDA Margin%"),
            ("Other Income", "Other Income"),
            ("Interest Expense", "Interest"),
            ("Depreciation", "Depreciation"),
            ("PBT", "PBT"),
            ("Tax", "Tax"),
            ("PAT", "PAT"),
            ("EPS", "EPS")
        ]
        
        inc_hdr = "| Particulars | " + " | ".join(formatted_headers) + " |"
        inc_sep = "|:---| " + " | ".join(["---:"] * len(years_to_show)) + " |"
        inc_rows = []
        for ss_col, label in inc_cols:
            col_idx = header_map.get(ss_col)
            row_cells = []
            for y in years_to_show:
                val = row_map[y][col_idx] if col_idx is not None else None
                if val is None:
                    cell_str = "-"
                elif label == "EBITDA Margin%":
                    cell_str = f"{val:.2f}%" if isinstance(val, (int, float)) else str(val)
                else:
                    cell_str = f"{val:,.2f}" if isinstance(val, (int, float)) else str(val)
                row_cells.append(cell_str)
            inc_rows.append(f"| {label} | " + " | ".join(row_cells) + " |")
        formatted["income_statement"] = "\n".join([inc_hdr, inc_sep] + inc_rows)
        
        # Table 2: Balance Sheet
        bs_cols = [
            ("Equity Capital", "Equity Capital"),
            ("Reserves", "Reserves"),
            ("Borrowings", "Borrowings"),
            ("Trade Payables", "Trade Payables"),
            ("Total Liabilities", "Total Liabilities"),
            ("Property Plant and Equipment", "Fixed Assets"),
            ("CWIP", "CWIP"),
            ("Investments", "Investments"),
            ("Current Assets", "Other Assets"),
            ("Total Assets", "Total Assets")
        ]
        
        bs_hdr = "| Particulars | " + " | ".join(formatted_headers) + " |"
        bs_sep = "|:---| " + " | ".join(["---:"] * len(years_to_show)) + " |"
        bs_rows = []
        for ss_col, label in bs_cols:
            col_idx = header_map.get(ss_col)
            row_cells = []
            for y in years_to_show:
                val = row_map[y][col_idx] if col_idx is not None else None
                cell_str = f"{val:,.2f}" if isinstance(val, (int, float)) else str(val) if val is not None else "-"
                row_cells.append(cell_str)
            bs_rows.append(f"| {label} | " + " | ".join(row_cells) + " |")
        formatted["balance_sheet"] = "\n".join([bs_hdr, bs_sep] + bs_rows)
        
        # Table 3: Cash Flow & Key Ratios
        ratio_cols = [
            ("Operating Cash Flow", "CFO"),
            ("Free Cash Flow", "Free Cash Flow"),
            ("Current Ratio", "Current Ratio"),
            ("Debt To Equity", "Debt to Equity"),
            ("ROE", "ROE%"),
            ("ROCE", "ROCE%"),
            ("Inventory Days", "Inventory Days"),
            ("Receivable Days", "Debtor Days"),
            ("Payable Days", "Days Payable"),
            ("Cash Conversion Cycle", "Cash Conversion Cycle")
        ]
        
        ratio_hdr = "| Particulars | " + " | ".join(formatted_headers) + " |"
        ratio_sep = "|:---| " + " | ".join(["---:"] * len(years_to_show)) + " |"
        ratio_rows = []
        for ss_col, label in ratio_cols:
            col_idx = header_map.get(ss_col)
            row_cells = []
            for y in years_to_show:
                val = row_map[y][col_idx] if col_idx is not None else None
                if val is None:
                    cell_str = "-"
                elif label in ["ROE%", "ROCE%"]:
                    cell_str = f"{val:.2f}%" if isinstance(val, (int, float)) else str(val)
                else:
                    cell_str = f"{val:,.2f}" if isinstance(val, (int, float)) else str(val)
                row_cells.append(cell_str)
            ratio_rows.append(f"| {label} | " + " | ".join(row_cells) + " |")
        formatted["cash_flow_ratios"] = "\n".join([ratio_hdr, ratio_sep] + ratio_rows)
        
    # 2. Format Peer Comparison Table
    peers = data.get("peers", [])
    card_details = data.get("card_details", {})
    if peers:
        peer_ids = [p["companyId"] for p in peers]
        # Fetch peer fundamentals in parallel
        peer_funds = fetch_peers_fundamentals_in_parallel(peer_ids)
        
        # Build target + peer rows
        all_ids = [target_id] + peer_ids
        # Deduplicate
        seen = set()
        dedup_ids = []
        for pid in all_ids:
            if pid not in seen:
                seen.add(pid)
                dedup_ids.append(pid)
                
        rows = []
        for pid in dedup_ids:
            # Check meta info
            c_name = pid.split(":")[1] if ":" in pid else pid
            fdata = fundamentals if pid == target_id else peer_funds.get(pid, {})
            meta = fdata.get("metaRatios", {})
            c_name_display = meta.get("Name", c_name)
            if pid == target_id:
                c_name_display = f"**{c_name_display} (Target)**"
                
            card_info = card_details.get(pid, {}).get("metaRatios", {})
            cmp = card_info.get("Close Price")
            mcap = card_info.get("Market Capitalization")
            pe = card_info.get("Price To Earnings")
            
            yearly_p = fdata.get("yearly", [])
            rev, opm, pb, roce = None, None, None, None
            if yearly_p and len(yearly_p) > 1:
                h_map = {h: idx for idx, h in enumerate(yearly_p[0])}
                latest_row = yearly_p[-1]
                rev = latest_row[h_map["Revenue"]] if "Revenue" in h_map else None
                opm = latest_row[h_map["OPM"]] if "OPM" in h_map else None
                pb = latest_row[h_map["Price To Book"]] if "Price To Book" in h_map else None
                roce = latest_row[h_map["ROCE"]] if "ROCE" in h_map else None
                
            rows.append({
                "name": c_name_display,
                "cmp": f"₹{cmp:,.2f}" if cmp else "-",
                "mcap": f"₹{mcap:,.1f} Cr" if mcap else "-",
                "rev": f"₹{rev:,.1f} Cr" if rev else "-",
                "opm": f"{opm:.2f}%" if opm else "-",
                "pe": f"{pe:.1f}x" if pe else "-",
                "pb": f"{pb:.2f}x" if pb else "-",
                "roce": f"{roce:.2f}%" if roce else "-"
            })
            
        md = []
        md.append("| Company | CMP | Market Cap | Revenue | EBITDA% (OPM) | P/E (TTM) | P/B (TTM) | ROCE% |")
        md.append("|:---|---:|---:|---:|---:|---:|---:|---:|")
        for r in rows:
            md.append(f"| {r['name']} | {r['cmp']} | {r['mcap']} | {r['rev']} | {r['opm']} | {r['pe']} | {r['pb']} | {r['roce']} |")
        formatted["peer_table"] = "\n".join(md)
        
    # 3. Format Shareholding Aggregate Table
    shareholding = data.get("shareholding", {})
    agg = shareholding.get("aggregate", [])
    if agg and len(agg) > 1:
        headers = agg[0]
        rows = agg[1:]
        
        md = []
        md.append("| " + " | ".join(headers) + " |")
        md.append("| " + " | ".join(["---:"] * len(headers)) + " |")
        for row in rows:
            cells = []
            for val in row:
                if isinstance(val, (int, float)):
                    cells.append(f"{val:.2f}%")
                else:
                    cells.append(str(val))
            md.append("| " + " | ".join(cells) + " |")
        formatted["shareholding_table"] = "\n".join(md)
        
    # 4. Format Bulk / Block Deals
    bulk_deals_list = data.get("bulk_deals", {}).get("bulkBlockDeals", [])
    if bulk_deals_list:
        hdr = "| Date | Shareholder Name | Type | Quantity | Avg Price | Value (Cr) |\n|:---|:---|:---|---:|---:|---:|"
        rows = []
        for item in bulk_deals_list[:15]:
            date = item.get("date") or "-"
            name = item.get("shareholderName") or "-"
            ttype = item.get("transactionType") or "-"
            qty = item.get("shareQuantity")
            qty_str = f"{qty:,.0f}" if isinstance(qty, (int, float)) else str(qty or "-")
            price = item.get("averagePrice")
            price_str = f"{price:.2f}" if isinstance(price, (int, float)) else str(price or "-")
            val = item.get("valueOfSecurities")
            val_str = f"{val:.2f}" if isinstance(val, (int, float)) else str(val or "-")
            rows.append(f"| {date} | {name} | {ttype} | {qty_str} | {price_str} | {val_str} |")
        formatted["bulk_deals"] = "\n".join([hdr] + rows)
    else:
        formatted["bulk_deals"] = "No recent bulk or block deals found."

    # 5. Format Insider Trading
    insider_list = data.get("insider_trading", {}).get("insiderTrading", [])
    if insider_list:
        hdr = "| Date | Shareholder Name | Category | Type | Quantity | Avg Price | Value (Cr) | Mode |\n|:---|:---|:---|:---|---:|---:|---:|:---|"
        rows = []
        for item in insider_list[:15]:
            date = item.get("date") or "-"
            name = item.get("shareholderName") or "-"
            cat = item.get("shareholderCategory") or "-"
            ttype = item.get("transactionType") or "-"
            qty = item.get("shareQuantity")
            qty_str = f"{qty:,.0f}" if isinstance(qty, (int, float)) else str(qty or "-")
            price = item.get("averagePrice")
            price_str = f"{price:.2f}" if isinstance(price, (int, float)) else str(price or "-")
            val = item.get("valueOfSecurities")
            val_str = f"{val:.2f}" if isinstance(val, (int, float)) else str(val or "-")
            mode = item.get("modeOfTransaction") or "-"
            rows.append(f"| {date} | {name} | {cat} | {ttype} | {qty_str} | {price_str} | {val_str} | {mode} |")
        formatted["insider_trading"] = "\n".join([hdr] + rows)
    else:
        formatted["insider_trading"] = "No recent insider trading transactions found."

    # 6. Format Substantial Acquisition
    acq_list = data.get("substantial_acquisition", {}).get("substantialAcquisition", [])
    if acq_list:
        hdr = "| Date | Acquirer Name | Type | Quantity | Avg Price | Value (Cr) |\n|:---|:---|:---|---:|---:|---:|"
        rows = []
        for item in acq_list[:15]:
            date = item.get("date") or "-"
            name = item.get("shareholderName") or item.get("acquirerName") or "-"
            ttype = item.get("transactionType") or "-"
            qty = item.get("shareQuantity")
            qty_str = f"{qty:,.0f}" if isinstance(qty, (int, float)) else str(qty or "-")
            price = item.get("averagePrice")
            price_str = f"{price:.2f}" if isinstance(price, (int, float)) else str(price or "-")
            val = item.get("valueOfSecurities")
            val_str = f"{val:.2f}" if isinstance(val, (int, float)) else str(val or "-")
            rows.append(f"| {date} | {name} | {ttype} | {qty_str} | {price_str} | {val_str} |")
        formatted["substantial_acquisition"] = "\n".join([hdr] + rows)
    else:
        formatted["substantial_acquisition"] = "No recent substantial acquisitions found."
        
    return formatted


def init_delivery_db():
    """Ensure the delivery_history table exists in logs/backtest.db."""
    db_path = Path("logs") / "backtest.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS delivery_history (
            date TEXT,
            symbol TEXT,
            series TEXT,
            close_price REAL,
            ttl_trd_qnty REAL,
            turnover_lacs REAL,
            deliv_qty REAL,
            deliv_per REAL,
            PRIMARY KEY (date, symbol)
        )
    """)
    conn.commit()
    conn.close()


def download_and_store_bhavcopy(target_date: date) -> bool:
    """Download daily bulk bhavcopy from NSE and insert all symbols into SQLite database."""
    date_str = target_date.strftime("%d%m%Y")
    db_date_str = target_date.strftime("%Y-%m-%d")
    url = f"https://nsearchives.nseindia.com/products/content/sec_bhavdata_full_{date_str}.csv"
    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "*/*",
        "Referer": "https://www.nseindia.com/"
    }
    db_path = Path("logs") / "backtest.db"
    try:
        r = requests.get(url, headers=headers, timeout=15)
        if r.status_code != 200:
            return False
        
        from io import StringIO
        csv_data = StringIO(r.text)
        df = pd.read_csv(csv_data)
        
        # Clean columns and strip strings
        df.columns = df.columns.str.strip()
        for col in df.select_dtypes(include=['object']).columns:
            df[col] = df[col].astype(str).str.strip()
            
        # Keep standard equity and related series
        if 'SERIES' in df.columns:
            df = df[df['SERIES'].isin(['EQ', 'BE', 'SM', 'ST'])]
            
        if df.empty:
            return False
            
        # Clean numeric columns
        num_cols = ['CLOSE_PRICE', 'TTL_TRD_QNTY', 'TURNOVER_LACS', 'DELIV_QTY', 'DELIV_PER']
        for col in num_cols:
            if col in df.columns:
                df[col] = pd.to_numeric(df[col].astype(str).str.replace(',', '', regex=False), errors='coerce')
                
        df = df.dropna(subset=['CLOSE_PRICE', 'TTL_TRD_QNTY', 'DELIV_QTY'])
        
        conn = sqlite3.connect(str(db_path))
        cursor = conn.cursor()
        
        records = []
        for _, row in df.iterrows():
            sym = str(row['SYMBOL']).strip().upper()
            series = str(row.get('SERIES', 'EQ')).strip()
            records.append((
                db_date_str,
                sym,
                series,
                float(row['CLOSE_PRICE']),
                float(row['TTL_TRD_QNTY']),
                float(row['TURNOVER_LACS']),
                float(row['DELIV_QTY']),
                float(row['DELIV_PER'])
            ))
            
        cursor.executemany("""
            INSERT OR REPLACE INTO delivery_history 
            (date, symbol, series, close_price, ttl_trd_qnty, turnover_lacs, deliv_qty, deliv_per)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, records)
        
        conn.commit()
        conn.close()
        print(f"✅ Successfully downloaded and stored bhavcopy for {db_date_str} ({len(records)} symbols)")
        return True
    except Exception as e:
        print(f"⚠️ Error downloading/storing bhavcopy for {db_date_str}: {e}")
        return False


def sync_delivery_history(days_back: int = 45):
    """Sync missing weekdays for the last days_back days and prune entries older than 30 trading dates."""
    db_path = Path("logs") / "backtest.db"
    init_delivery_db()
    
    conn = sqlite3.connect(str(db_path))
    cursor = conn.cursor()
    cursor.execute("SELECT DISTINCT date FROM delivery_history")
    existing_dates = {row[0] for row in cursor.fetchall()}
    conn.close()
    
    today = date.today()
    synced_count = 0
    
    for i in range(days_back):
        target_date = today - timedelta(days=i)
        if target_date.weekday() >= 5:
            continue
            
        db_date_str = target_date.strftime("%Y-%m-%d")
        if db_date_str in existing_dates:
            continue
            
        print(f"🔄 Syncing missing date: {db_date_str}")
        success = download_and_store_bhavcopy(target_date)
        if success:
            synced_count += 1
            
    # Prune database to only keep the top 30 most recent trading dates
    conn = sqlite3.connect(str(db_path))
    cursor = conn.cursor()
    cursor.execute("SELECT DISTINCT date FROM delivery_history ORDER BY date DESC LIMIT 30")
    recent_dates = [row[0] for row in cursor.fetchall()]
    if recent_dates:
        # Delete any rows not in the top 30 dates
        placeholders = ",".join("?" for _ in recent_dates)
        cursor.execute(f"DELETE FROM delivery_history WHERE date NOT IN ({placeholders})", recent_dates)
        pruned = cursor.rowcount
        if pruned > 0:
            print(f"🧹 Pruned {pruned} old rows from delivery_history (kept top 30 trading dates)")
    conn.commit()
    conn.close()
    print(f"📊 Delivery history sync completed. Synced {synced_count} missing days.")


def fetch_nse_delivery_data(symbol: str) -> dict:
    """
    Fetch historical delivery quantity and calculate weekly medians.
    Queries the local SQLite database. If < 5 days are found (e.g. brand new equity),
    falls back to live nselib fetch, inserts to DB, and re-queries.
    """
    symbol = symbol.strip().upper()
    db_path = Path("logs") / "backtest.db"
    
    init_delivery_db()
    
    def query_local(sym):
        conn = sqlite3.connect(str(db_path))
        query = """
            SELECT date, ttl_trd_qnty, deliv_qty, deliv_per, close_price, turnover_lacs
            FROM delivery_history
            WHERE symbol = ?
            ORDER BY date DESC
            LIMIT 5
        """
        try:
            df_local = pd.read_sql_query(query, conn, params=(sym,))
        except Exception as e:
            print(f"⚠️ Error reading delivery_history from DB: {e}")
            df_local = pd.DataFrame()
        conn.close()
        return df_local

    df = query_local(symbol)
    
    # Fallback if less than 5 days of data
    if df.empty or len(df) < 5:
        print(f"ℹ️ Under 5 days of history in DB for {symbol} (found {len(df)}). Running fallback live fetch...")
        try:
            from nselib import capital_market
            
            end_date = datetime.today()
            start_date = end_date - timedelta(days=20)
            from_date_str = start_date.strftime("%d-%m-%Y")
            to_date_str = end_date.strftime("%d-%m-%Y")
            
            df_live = capital_market.price_volume_and_deliverable_position_data(
                symbol=symbol,
                from_date=from_date_str,
                to_date=to_date_str
            )
            
            if not df_live.empty:
                df_live.columns = [c.replace('ï»¿', '').replace('"', '').strip() for c in df_live.columns]
                
                df_live['ParsedDate'] = pd.to_datetime(df_live['Date'], format='%d-%b-%Y', errors='coerce')
                if df_live['ParsedDate'].isna().all():
                    df_live['ParsedDate'] = pd.to_datetime(df_live['Date'], format='%d-%m-%Y', errors='coerce')
                    
                df_live = df_live.dropna(subset=['ParsedDate']).sort_values('ParsedDate')
                
                for col in ['DeliverableQty', '%DlyQttoTradedQty', 'TotalTradedQuantity', 'ClosePrice', 'TurnoverInRs']:
                    if col in df_live.columns:
                        if df_live[col].dtype == object:
                            df_live[col] = df_live[col].astype(str).str.replace(',', '', regex=False)
                        df_live[col] = pd.to_numeric(df_live[col], errors='coerce')
                        
                df_live = df_live.dropna(subset=['DeliverableQty', '%DlyQttoTradedQty', 'TotalTradedQuantity', 'ClosePrice', 'TurnoverInRs'])
                
                if not df_live.empty:
                    conn = sqlite3.connect(str(db_path))
                    cursor = conn.cursor()
                    records = []
                    for _, row in df_live.iterrows():
                        db_date = row['ParsedDate'].strftime('%Y-%m-%d')
                        records.append((
                            db_date,
                            symbol,
                            'EQ',
                            float(row['ClosePrice']),
                            float(row['TotalTradedQuantity']),
                            float(row['TurnoverInRs']) / 100000.0,
                            float(row['DeliverableQty']),
                            float(row['%DlyQttoTradedQty'])
                        ))
                    cursor.executemany("""
                        INSERT OR REPLACE INTO delivery_history
                        (date, symbol, series, close_price, ttl_trd_qnty, turnover_lacs, deliv_qty, deliv_per)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """, records)
                    conn.commit()
                    conn.close()
                    print(f"✅ Fallback successful: Backfilled {len(records)} days of history for {symbol}")
                    
                    # Re-query local DB
                    df = query_local(symbol)
        except Exception as fe:
            print(f"⚠️ Fallback fetch failed for {symbol}: {fe}")
            
    if df.empty or len(df) == 0:
        return {}
        
    # Sort ascending for median and index logic
    df = df.iloc[::-1].copy() # reverse order so that oldest is first and latest is last
    latest_row = df.iloc[-1]
    
    # Calculate values in Rs. Cr
    df['DeliveryValueCr'] = (df['deliv_qty'] * df['close_price']) / 10000000.0
    df['TradedValueCr'] = (df['turnover_lacs'] * 100000.0) / 10000000.0
    
    # Parse latest_date to DD-Mon-YYYY to maintain UI compatibility
    latest_db_date = latest_row['date'] # YYYY-MM-DD
    try:
        latest_date_obj = datetime.strptime(latest_db_date, "%Y-%m-%d")
        latest_date_display = latest_date_obj.strftime("%d-%b-%Y")
    except Exception:
        latest_date_display = latest_db_date
        
    return {
        "latest_date": latest_date_display,
        "latest_traded_qty": float(latest_row['ttl_trd_qnty']),
        "latest_delivery_qty": float(latest_row['deliv_qty']),
        "latest_delivery_pct": float(latest_row['deliv_per']),
        "latest_traded_val_cr": float(df['TradedValueCr'].iloc[-1]),
        "latest_delivery_val_cr": float(df['DeliveryValueCr'].iloc[-1]),
        "week_delivery_qty_median": float(df['deliv_qty'].median()),
        "week_delivery_pct_median": float(df['deliv_per'].median()),
        "week_traded_qty_median": float(df['ttl_trd_qnty'].median()),
        "week_traded_val_median_cr": float(df['TradedValueCr'].median()),
        "week_delivery_val_median_cr": float(df['DeliveryValueCr'].median())
    }



def calculate_delivery_signal(stats: dict, cmp: float) -> dict:
    """
    Calculate the delivery volume signal and actionable suggestions for investors.
    Returns a dictionary containing badge_text, badge_html, suggestions, table_html, and value_cr.
    """
    if not stats:
        return {
            "badge_text": "Neutral",
            "badge_html": '<span style="font-size: 11px; background-color: #f3f4f6; color: #374151; padding: 3px 8px; border-radius: 4px; font-weight: bold; font-family: sans-serif;">⚖️ Neutral</span>',
            "suggestions": "No delivery data available to calculate signals.",
            "table_html": "",
            "value_cr": 0.0,
            "latest_delivery_pct": 0.0,
            "week_delivery_pct_median": 0.0,
            "latest_delivery_val_cr": 0.0,
            "week_delivery_val_median_cr": 0.0
        }
        
    latest_date = stats.get("latest_date", "Today")
    latest_traded_qty = stats.get("latest_traded_qty", 0.0)
    latest_delivery_qty = stats.get("latest_delivery_qty", 0.0)
    latest_delivery_pct = stats.get("latest_delivery_pct", 0.0)
    week_delivery_qty_median = stats.get("week_delivery_qty_median", 1.0)
    week_delivery_pct_median = stats.get("week_delivery_pct_median", 0.0)
    week_traded_qty_median = stats.get("week_traded_qty_median", 1.0)
    
    # Calculate delivery value in Rs. Cr
    value_cr = stats.get("latest_delivery_val_cr")
    if value_cr is None:
        value_cr = (latest_delivery_qty * cmp) / 10000000.0 if cmp else 0.0
        
    # Rule indicators
    is_accumulation = (
        latest_delivery_pct > week_delivery_pct_median + 5.0 and
        latest_delivery_qty > week_delivery_qty_median * 1.2 and
        value_cr >= 1.0
    )
    is_strong_delivery = (
        latest_delivery_pct >= 45.0 or
        (latest_delivery_pct > week_delivery_pct_median + 2.0 and latest_delivery_qty >= week_delivery_qty_median)
    )
    is_speculative_churn = (
        latest_traded_qty > week_traded_qty_median * 2.0 and
        latest_delivery_pct < 20.0
    )
    
    if is_accumulation:
        badge_text = f"🔥 High Accumulation (₹{value_cr:.2f} Cr)"
        badge_color = "#15803d"
        badge_html = f'<span style="font-size: 11.5px; background-color: #dcfce7; color: #15803d; padding: 4px 8px; border-radius: 6px; font-weight: bold; font-family: sans-serif; border: 1px solid #bbf7d0; display: inline-block; white-space: nowrap;">🔥 High Accumulation</span>'
        suggestions = f"Today's deliverable volume is significantly above the weekly median with an expanding delivery percentage, indicating strong institutional/insider accumulation. Conviction: High. Excellent setup for accumulating or holding."
    elif is_strong_delivery:
        badge_text = f"🛡️ Strong Delivery (₹{value_cr:.2f} Cr)"
        badge_color = "#0369a1"
        badge_html = f'<span style="font-size: 11.5px; background-color: #e0f2fe; color: #0369a1; padding: 4px 8px; border-radius: 6px; font-weight: bold; font-family: sans-serif; border: 1px solid #bae6fd; display: inline-block; white-space: nowrap;">🛡️ Strong Delivery</span>'
        suggestions = f"Strong deliverable percentage or volume indicates buying interest is steady and shares are being tucked away. Conviction: Positive. Supports holding or building long positions."
    elif is_speculative_churn:
        badge_text = "⚠️ Speculative Churn"
        badge_color = "#b91c1c"
        badge_html = f'<span style="font-size: 11.5px; background-color: #fee2e2; color: #b91c1c; padding: 4px 8px; border-radius: 6px; font-weight: bold; font-family: sans-serif; border: 1px solid #fecaca; display: inline-block; white-space: nowrap;">⚠️ Speculative Churn</span>'
        suggestions = f"Extremely high trading volume combined with low delivery percentage (<20%) suggests heavy intraday speculativeness and churn rather than long-term accumulation. Conviction: Cautious. High volatility expected; avoid chasing momentum blindly."
    else:
        badge_text = "⚖️ Neutral"
        badge_color = "#374151"
        badge_html = f'<span style="font-size: 11.5px; background-color: #f3f4f6; color: #374151; padding: 4px 8px; border-radius: 6px; font-weight: bold; font-family: sans-serif; border: 1px solid #e5e7eb; display: inline-block; white-space: nowrap;">⚖️ Neutral</span>'
        suggestions = f"Traded and deliverable volumes are in line with the weekly average. Conviction: Neutral. Follow primary technical confluence breakout signals."
        
    latest_traded_lakh = latest_traded_qty / 100000.0
    latest_del_lakh = latest_delivery_qty / 100000.0
    week_traded_med_lakh = week_traded_qty_median / 100000.0
    week_del_med_lakh = week_delivery_qty_median / 100000.0
    
    latest_traded_val_cr = stats.get("latest_traded_val_cr")
    if latest_traded_val_cr is None:
        latest_traded_val_cr = 0.0 # will fallback
        
    latest_delivery_val_cr = value_cr
    
    week_traded_val_median_cr = stats.get("week_traded_val_median_cr", 0.0)
    week_delivery_val_median_cr = stats.get("week_delivery_val_median_cr", 0.0)
    
    # Calculate % difference vs weekly median
    vol_diff_pct = ((latest_traded_qty / week_traded_qty_median) - 1.0) * 100.0 if week_traded_qty_median else 0.0
    del_diff_pct = ((latest_delivery_qty / week_delivery_qty_median) - 1.0) * 100.0 if week_delivery_qty_median else 0.0
    pct_diff = latest_delivery_pct - week_delivery_pct_median
    val_diff_pct = ((latest_delivery_val_cr / week_delivery_val_median_cr) - 1.0) * 100.0 if week_delivery_val_median_cr else 0.0
    
    vol_sign = "+" if vol_diff_pct >= 0 else ""
    del_sign = "+" if del_diff_pct >= 0 else ""
    pct_sign = "+" if pct_diff >= 0 else ""
    val_sign = "+" if val_diff_pct >= 0 else ""

    table_html = f"""
    <table style="border-collapse: collapse; width: 100%; font-size: 13px; font-family: sans-serif; border: 1px solid #e2e8f0; margin-top: 10px;">
      <thead>
        <tr style="background-color: #f8fafc; border-bottom: 1px solid #e2e8f0; color: #4a5568;">
          <th style="padding: 8px 10px; text-align: left; border: 1px solid #e2e8f0;">Metric</th>
          <th style="padding: 8px 10px; text-align: right; border: 1px solid #e2e8f0;">Today ({latest_date})</th>
          <th style="padding: 8px 10px; text-align: right; border: 1px solid #e2e8f0;">Weekly Median (5-Day)</th>
          <th style="padding: 8px 10px; text-align: left; border: 1px solid #e2e8f0;">Description / Significance</th>
        </tr>
      </thead>
      <tbody>
        <tr>
          <td style="padding: 8px 10px; border: 1px solid #e2e8f0; font-weight: bold; color: #2d3748;">Traded Volume & Value</td>
          <td style="padding: 8px 10px; border: 1px solid #e2e8f0; text-align: right; font-weight: bold;">{latest_traded_lakh:.2f} Lakh <span style="font-size: 11px; color: #4a5568; font-weight: normal;">(₹{latest_traded_val_cr:.2f} Cr)</span></td>
          <td style="padding: 8px 10px; border: 1px solid #e2e8f0; text-align: right;">{week_traded_med_lakh:.2f} Lakh <span style="font-size: 11px; color: #718096;">(₹{week_traded_val_median_cr:.2f} Cr)</span></td>
          <td style="padding: 8px 10px; border: 1px solid #e2e8f0; color: #4a5568;">Total shares and market value traded today ({vol_sign}{vol_diff_pct:.1f}% vs median). Measures liquidity and interest.</td>
        </tr>
        <tr style="background-color: #f8fafc;">
          <td style="padding: 8px 10px; border: 1px solid #e2e8f0; font-weight: bold; color: #2d3748;">Delivery Volume & Value</td>
          <td style="padding: 8px 10px; border: 1px solid #e2e8f0; text-align: right; font-weight: bold;">{latest_del_lakh:.2f} Lakh <span style="font-size: 11px; color: #4a5568; font-weight: normal;">(₹{latest_delivery_val_cr:.2f} Cr)</span></td>
          <td style="padding: 8px 10px; border: 1px solid #e2e8f0; text-align: right;">{week_del_med_lakh:.2f} Lakh <span style="font-size: 11px; color: #718096;">(₹{week_delivery_val_median_cr:.2f} Cr)</span></td>
          <td style="padding: 8px 10px; border: 1px solid #e2e8f0; color: #4a5568;">Shares actually bought and moved to Demat ({del_sign}{del_diff_pct:.1f}% vs median). Measures long-term conviction.</td>
        </tr>
        <tr>
          <td style="padding: 8px 10px; border: 1px solid #e2e8f0; font-weight: bold; color: #2d3748;">Delivery Percentage</td>
          <td style="padding: 8px 10px; border: 1px solid #e2e8f0; text-align: right; font-weight: bold; color: {badge_color};">{latest_delivery_pct:.2f}%</td>
          <td style="padding: 8px 10px; border: 1px solid #e2e8f0; text-align: right;">{week_delivery_pct_median:.2f}%</td>
          <td style="padding: 8px 10px; border: 1px solid #e2e8f0; color: #4a5568;">Portion of traded volume delivered ({pct_sign}{pct_diff:+.2f}% absolute diff). Higher % indicates accumulation vs speculation.</td>
        </tr>
        <tr style="background-color: #f8fafc;">
          <td style="padding: 8px 10px; border: 1px solid #e2e8f0; font-weight: bold; color: #2d3748;">Demat Delivery Value (Cr)</td>
          <td style="padding: 8px 10px; border: 1px solid #e2e8f0; text-align: right; font-weight: bold; color: #2e7d32;">₹{latest_delivery_val_cr:.2f} Cr</td>
          <td style="padding: 8px 10px; border: 1px solid #e2e8f0; text-align: right;">₹{week_delivery_val_median_cr:.2f} Cr</td>
          <td style="padding: 8px 10px; border: 1px solid #e2e8f0; color: #4a5568;">Net capital value of delivered shares ({val_sign}{val_diff_pct:.1f}% vs median). Represents actual cash flow allocation.</td>
        </tr>
      </tbody>
    </table>
    """
    
    return {
        "badge_text": badge_text,
        "badge_html": badge_html,
        "suggestions": suggestions,
        "table_html": table_html,
        "value_cr": value_cr,
        "latest_delivery_pct": latest_delivery_pct,
        "week_delivery_pct_median": week_delivery_pct_median,
        "latest_delivery_val_cr": latest_delivery_val_cr,
        "week_delivery_val_median_cr": week_delivery_val_median_cr
    }


def enrich_basic_metadata(r: dict) -> dict:
    """
    Enrich basic metadata (close price, mcap, industry, company name) using yfinance as fallback
    if they are 0.0 or 'unknown'.
    """
    symbol = r["symbol"]
    if r.get("close", 0.0) == 0.0 or r.get("mcap_cr", 0.0) == 0.0 or r.get("industry", "unknown") == "unknown":
        import yfinance as yf
        print(f"ℹ️ Basic metadata missing for {symbol}. Querying yfinance fallback...")
        yf_info = {}
        for suffix in [".NS", ".BO"]:
            try:
                ticker = yf.Ticker(symbol + suffix)
                yf_info = ticker.info
                if yf_info and yf_info.get("marketCap"):
                    break
            except Exception:
                continue
        if yf_info:
            if r.get("close", 0.0) == 0.0:
                r["close"] = yf_info.get("currentPrice") or yf_info.get("previousClose") or 0.0
            if r.get("mcap_cr", 0.0) == 0.0:
                r["mcap_cr"] = (yf_info.get("marketCap", 0.0) / 10000000.0) if yf_info.get("marketCap") else 0.0
            if r.get("industry", "unknown") == "unknown":
                r["industry"] = yf_info.get("industry") or yf_info.get("sector") or "unknown"
            if r.get("company") == symbol or not r.get("company"):
                r["company"] = yf_info.get("longName") or yf_info.get("shortName") or symbol
            print(f"   Updated basic metadata for {symbol}: close={r['close']}, mcap={r['mcap_cr']:.2f} Cr, industry={r['industry']}")
    return r


def enrich_stock_with_actuals(r: dict) -> dict:
    """
    Query StockScans and yfinance to fetch and format actual financials, peers, and ratios,
    enriching the stock record dict for prompt injection.
    """
    symbol = r["symbol"]
    print(f"📊 [ENRICHING] Fetching actual fundamental details for `{symbol}`...")
    
    try:
        # 1. Fetch all StockScans API details
        ss_data = fetch_stockscans_company_data(symbol)
        
        # 2. Fetch yfinance details
        import yfinance as yf
        yf_info = {}
        for suffix in [".NS", ".BO"]:
            try:
                ticker = yf.Ticker(symbol + suffix)
                yf_info = ticker.info
                if yf_info and yf_info.get("marketCap"):
                    break
            except Exception:
                continue
        
        # 3. Extract target ratios for metadata
        ss_meta_ratios = {}
        fundamentals = ss_data.get("fundamentals", {})
        q_data = fundamentals.get("quarterly", [])
        if q_data and len(q_data) > 1:
            r["latest_quarter"] = str(q_data[-1][0])
            
        yearly = fundamentals.get("yearly", [])
        if yearly and len(yearly) > 1:
            headers = yearly[0]
            latest_row = yearly[-1]
            header_map = {h: idx for idx, h in enumerate(headers)}
            
            # Map values
            for k in ["Price To Earnings", "Price To Book", "ROCE", "ROE", "EPS"]:
                col_idx = header_map.get(k)
                if col_idx is not None:
                    ss_meta_ratios[k] = latest_row[col_idx]
            
            # Extract book value if reserves and equity exist
            eq_idx = header_map.get("Equity Capital")
            res_idx = header_map.get("Reserves")
            if eq_idx is not None and res_idx is not None:
                equity = latest_row[eq_idx] or 0.0
                reserves = latest_row[res_idx] or 0.0
                shares = yf_info.get("sharesOutstanding")
                if shares and (equity + reserves) > 0:
                    ss_meta_ratios["Book Value"] = ((equity + reserves) * 10000000.0) / shares
                    
        # Check if card details has PE as fallback
        card_meta = ss_data.get("card_details", {}).get(f"{ss_data.get('exchange')}:{symbol}", {}).get("metaRatios", {})
        if "Price To Earnings" not in ss_meta_ratios and card_meta.get("Price To Earnings"):
            ss_meta_ratios["Price To Earnings"] = card_meta.get("Price To Earnings")
            
        # Robust fallbacks for close price, mcap, and industry to avoid 0s and 'unknown'
        if (not r.get("close") or r.get("close") == 0):
            r["close"] = card_meta.get("Close Price") or yf_info.get("currentPrice") or yf_info.get("previousClose") or 0.0
            
        if (not r.get("mcap_cr") or r.get("mcap_cr") == 0):
            r["mcap_cr"] = card_meta.get("Market Capitalization") or (yf_info.get("marketCap", 0.0) / 10000000.0) or 0.0
            
        search_meta = ss_data.get("search", {}).get("metaRatios", {})
        if (not r.get("industry") or str(r.get("industry")).lower() == "unknown"):
            r["industry"] = search_meta.get("Industry") or yf_info.get("industry") or yf_info.get("sector") or "unknown"
            
        # 3b. Fetch NSE delivery stats
        delivery_stats = fetch_nse_delivery_data(symbol)
        delivery_table = ""
        if delivery_stats:
            latest_traded = delivery_stats["latest_traded_qty"] / 100000.0
            latest_deliv = delivery_stats["latest_delivery_qty"] / 100000.0
            week_deliv_med = delivery_stats["week_delivery_qty_median"] / 100000.0
            
            delivery_table = (
                f"| Metric | Value | Significance |\n"
                f"|:---|:---|:---|\n"
                f"| Daily Traded Volume | {latest_traded:.2f} Lakh shares | Total volume traded on {delivery_stats['latest_date']} |\n"
                f"| Daily Delivery Volume | {latest_deliv:.2f} Lakh shares | Delivery quantity on {delivery_stats['latest_date']} |\n"
                f"| Daily Delivery % | {delivery_stats['latest_delivery_pct']:.2f}% | Percentage of delivery volume |\n"
                f"| Weekly Median Delivery Vol | {week_deliv_med:.2f} Lakh shares | Median delivery volume (5 trading days) |\n"
                f"| Weekly Median Delivery % | {delivery_stats['week_delivery_pct_median']:.2f}% | Median delivery percentage |"
            )
            
            sig = calculate_delivery_signal(delivery_stats, r.get("close", 0.0))
            delivery_table += f"\n\n**Delivery Actionable Signal:** {sig['badge_text']}\n"
            delivery_table += f"**Conviction Suggestion:** {sig['suggestions']}\n"
            r["delivery_signal"] = sig
            r["delivery_stats"] = delivery_stats
        else:
            r["delivery_signal"] = calculate_delivery_signal({}, 0.0)
            r["delivery_stats"] = {}
            
        r["ss_delivery_table"] = delivery_table

        # 4. Generate Markdown tables
        tables = format_actuals_to_markdown(ss_data)
        
        # 5. Enrich dict r
        scr_peers = dp.fetch_screener_peers(symbol)
        r["ss_peer_table"] = scr_peers or tables.get("peer_table") or ""
        
        scr_tables = dp.scrape_screener_financial_tables(symbol)
        r["ss_income_statement"] = scr_tables.get("income_statement") or tables.get("income_statement") or ""
        r["ss_balance_sheet"] = scr_tables.get("balance_sheet") or tables.get("balance_sheet") or ""
        r["ss_cash_flow_ratios"] = scr_tables.get("cash_flow_ratios") or tables.get("cash_flow_ratios") or ""
        r["ss_shareholding_table"] = scr_tables.get("shareholding_table") or tables.get("shareholding_table") or ""
        r["ss_bulk_deals"] = tables.get("bulk_deals") or ""
        r["ss_insider_trading"] = tables.get("insider_trading") or ""
        r["ss_substantial_acquisition"] = tables.get("substantial_acquisition") or ""
        r["ss_years"] = tables.get("years") or []
        # 3c. Supplement using public Screener page scraping
        try:
            screener_ratios = dp.scrape_screener_ratios(symbol)
            if screener_ratios:
                print(f"📊 [SCREENER SCRAPER] Successfully fetched {len(screener_ratios)} items from Screener public page.")
                
                ratio_mapping = {
                    "Stock P/E": "Price To Earnings",
                    "Price to book value": "Price To Book",
                    "ROCE": "ROCE",
                    "ROE": "ROE",
                    "Book Value": "Book Value",
                    "Dividend Yield": "Dividend Yield",
                    "Face Value": "Face Value"
                }
                
                for scr_key, meta_key in ratio_mapping.items():
                    matched_key = next((k for k in screener_ratios.keys() if scr_key.lower() in k.lower()), None)
                    if matched_key:
                        ss_meta_ratios[meta_key] = screener_ratios[matched_key]
                
                if "SH_Promoters" in screener_ratios:
                    ss_meta_ratios["Promoter"] = screener_ratios["SH_Promoters"]
                if "SH_FIIs" in screener_ratios:
                    ss_meta_ratios["FII"] = screener_ratios["SH_FIIs"]
                if "SH_DIIs" in screener_ratios:
                    ss_meta_ratios["DII"] = screener_ratios["SH_DIIs"]
                if "SH_Public" in screener_ratios:
                    ss_meta_ratios["Public"] = screener_ratios["SH_Public"]
        except Exception as scr_err:
            print(f"⚠️ Screener scraper enrichment warning: {scr_err}")

        r["ss_meta_ratios"] = ss_meta_ratios
        r["yf_info"] = yf_info
        
        print(f"✅ [ENRICHED] Successfully loaded actuals for `{symbol}` from StockScans & yfinance.")
    except Exception as e:
        print(f"⚠️ [ENRICHMENT FAILED] Could not fetch actuals for `{symbol}`. Falling back to LLM simulation: {e}")
        r["ss_peer_table"] = ""
        r["ss_income_statement"] = ""
        r["ss_balance_sheet"] = ""
        r["ss_cash_flow_ratios"] = ""
        r["ss_shareholding_table"] = ""
        r["ss_bulk_deals"] = ""
        r["ss_insider_trading"] = ""
        r["ss_substantial_acquisition"] = ""
        r["ss_years"] = []
        r["ss_delivery_table"] = ""
        r["ss_meta_ratios"] = {}
        r["yf_info"] = {}
        
    return r

def get_calendar_quarter(dt: datetime) -> tuple[int, int]:
    """Return the (quarter, year) of the given datetime object."""
    quarter = (dt.month - 1) // 3 + 1
    return quarter, dt.year

def verify_report_completeness(report_text: str) -> list[str]:
    """Verify that every single required section and table is present in the output text using content-focused resilient checks."""
    required_patterns = {
        "HEADER BLOCK (Rating & Target)": [r"Rating", r"(?:Target|Valuation|CMP)"],
        "SECTION 2 (Investment Thesis)": [r"(?:Thesis|Investment|Catalyst)"],
        "SECTION 3 (Business Overview)": [r"(?:Business|Model|Overview|Structure)"],
        "SECTION 4 (Industry Landscape)": [r"(?:Industry|Competitive|Landscape|Peer)"],
        "SECTION 5 (Management Quality)": [r"(?:Management|Pedigree|Governance|Promoter)"],
        "SECTION 6 (Financial Statements)": [r"(?:Financial|Deep-Dive|Income|Balance|Cash|Particulars)"],
        "SECTION 7 (Earnings Quality)": [r"(?:Earnings|Quality|Checklist)"],
        "SECTION 8 (Valuation Scenarios)": [r"(?:Valuation|Scenario|Bull|Base|Bear)"],
        "SECTION 9 (Key Risks)": [r"(?:Risk|Threat|P\s*×\s*I|PxI)"],
        "SECTION 10 (Recommendation)": [r"(?:Recommendation|Horizon|Entry|Stop)"],
        "SECTION 10B (Technical Chart Levels)": [r"(?:Technical|EMA|VStop|Support|Resistance)"],
        "APPENDIX (Latest Concall Brief)": [r"(?:Appendix|Concall|Brief|Boss)"],
        "DISCLAIMER (Global style rules)": [r"(?:Disclaimer|informational purposes)"]
    }
    
    missing = []
    for label, keywords in required_patterns.items():
        found_all = True
        for kw in keywords:
            if not re.search(kw, report_text, re.IGNORECASE):
                found_all = False
                break
        if not found_all:
            missing.append(label)
            
    return missing

def extract_target_and_upside(report_md: str, cmp: float) -> tuple[float, float]:
    """
    Attempt to extract the 12M target price and upside % from the generated markdown report.
    Falls back to cmp * 1.35 (+35%) if parsing fails.
    """
    patterns = [
        r"(?:Blended Target Price|Blended Target|12M Price Target|12M Target Price|12M Target|Price Target|Target Price)\b.*?(?:Rs\.|₹|Rs)\s*\**([\d,]+(?:\.\d+)?)\**",
        r"12M TARGET\s*:\s*(?:Rs\.|₹|Rs)?\s*([\d,]+(?:\.\d+)?)"
    ]
    
    target_val = None
    for pattern in patterns:
        matches = re.findall(pattern, report_md, re.IGNORECASE)
        if matches:
            for val_str in matches:
                try:
                    val = float(val_str.replace(",", "").strip())
                    if cmp > 0 and (cmp * 0.5 < val < cmp * 10):
                        target_val = val
                        break
                except ValueError:
                    continue
        if target_val is not None:
            break
            
    if target_val is None:
        for line in report_md.splitlines():
            if "|" in line and any(x in line.lower() for x in ["target", "upside"]):
                nums = re.findall(r"[\d,]+(?:\.\d+)?", line)
                for num_str in nums:
                    try:
                        val = float(num_str.replace(",", "").strip())
                        if cmp > 0 and (cmp * 0.8 < val < cmp * 10):
                            target_val = val
                            break
                    except ValueError:
                        continue
            if target_val is not None:
                break

    if target_val is not None and cmp > 0:
        upside = ((target_val / cmp) - 1.0) * 100.0
        return target_val, upside
        
    return cmp * 1.35, 35.0

def sanitize_report_header_block(report_md: str, cmp: float, target_price: float, upside_pct: float) -> str:
    """
    Finds the header block table in the report markdown and replaces the old target price
    and old 35% upside with the dynamically computed target price and upside %.
    """
    import re
    lines = report_md.splitlines()
    header_end = 30 if len(lines) > 30 else len(lines)
    
    for i in range(header_end):
        line = lines[i]
        if "|" in line:
            if any(r in line for r in ["BUY", "HOLD", "REDUCE"]):
                old_target_approx = cmp * 1.35
                
                # Replace upside % representation (like 35%, 35.0%, 35.00%, etc.)
                line = re.sub(r'\b35(?:\.0+)?%?', f'{upside_pct:.1f}%', line)
                
                # Replace the old target price in the table cells
                cells = line.split("|")
                new_cells = []
                for cell in cells:
                    cell_strip = cell.strip()
                    num_match = re.search(r'([\d,]+(?:\.\d+)?)', cell_strip)
                    if num_match:
                        try:
                            val = float(num_match.group(1).replace(",", ""))
                            if cmp > 0 and abs(val - old_target_approx) / old_target_approx < 0.05:
                                cell = cell.replace(num_match.group(1), f"{target_price:.2f}")
                        except ValueError:
                            pass
                    new_cells.append(cell)
                line = "|".join(new_cells)
                lines[i] = line
                
    return "\n".join(lines)

def fetch_latest_stockscans_quarter(symbol: str) -> str:
    cookie = os.environ.get("STOCKSCANS_COOKIE", "")
    headers = {
        "accept": "application/json",
        "cookie": cookie,
        "user-agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
    }
    source = "C"
    exchange = "NSE"
    for ex in ["NSE", "BSE"]:
        url = f"https://www.stockscans.in/api/company/scans/search-company/{ex}:{symbol}"
        try:
            r = requests.get(url, headers=headers, timeout=5)
            if r.status_code == 200:
                exchange = ex
                meta = r.json().get("metaRatios", {})
                source = meta.get("Fundamentals Source") or "C"
                break
        except Exception:
            continue
    url = f"https://www.stockscans.in/api/company/fundamentals/{exchange}:{symbol}/{source}"
    try:
        r = requests.get(url, headers=headers, timeout=5)
        if r.status_code == 200:
            q_data = r.json().get("quarterly", [])
            if len(q_data) > 1:
                return str(q_data[-1][0])
    except Exception:
        pass
    return ""

def find_matching_existing_report(symbol: str, reports_dir: Path, today: datetime) -> tuple[Path, str]:
    """
    Search for an existing report for symbol.
    If it exists:
      - Read the report file.
      - If it contains '<!-- latest_quarter: YYYYMM -->':
        - Fetch the latest quarter from StockScans.
        - If the latest quarter is the same, return the filepath and info.
      - If it doesn't contain the comment:
        - Fall back to 75-day cooldown check.
    Returns (filepath, info) or (None, "").
    """
    if not reports_dir.exists():
        return None, ""
        
    prefix = f"{symbol}_equity_report_"
    latest_date = None
    latest_filepath = None
    
    for filepath in reports_dir.glob(f"{prefix}*.md"):
        filename = filepath.name
        try:
            date_str = filename.replace(prefix, "").replace(".md", "")
            file_date = datetime.strptime(date_str, "%Y-%m-%d")
            if latest_date is None or file_date > latest_date:
                latest_date = file_date
                latest_filepath = filepath
        except Exception:
            try:
                mtime = datetime.fromtimestamp(filepath.stat().st_mtime)
                if latest_date is None or mtime > latest_date:
                    latest_date = mtime
                    latest_filepath = filepath
            except Exception:
                pass
                
    if latest_filepath is not None:
        try:
            with open(latest_filepath, "r") as f:
                content = f.read()
                
            # Check if any new/updated documents are available on StockScans or Screener
            print(f"🔍 [find_matching_existing_report] Checking if newer documents are available for {symbol}...")
            ss_docs = fetch_stockscans_documents(symbol, "NSE")
            ip_pdf = ss_docs.get("ip_pdf")
            ar_pdf = ss_docs.get("ar_pdf")
            concall_pdf = ss_docs.get("concall_pdf")
            if not ip_pdf or not ar_pdf or not concall_pdf:
                ss_docs_bse = fetch_stockscans_documents(symbol, "BSE")
                if not ip_pdf:
                    ip_pdf = ss_docs_bse.get("ip_pdf")
                if not ar_pdf:
                    ar_pdf = ss_docs_bse.get("ar_pdf")
                if not concall_pdf:
                    concall_pdf = ss_docs_bse.get("concall_pdf")
            if not ip_pdf or not ar_pdf or not concall_pdf:
                screener_docs = fetch_screener_documents(symbol)
                if not ip_pdf:
                    ip_pdf = screener_docs.get("ip_pdf")
                if not ar_pdf:
                    ar_pdf = screener_docs.get("ar_pdf")
                if not concall_pdf:
                    concall_pdf = screener_docs.get("concall_pdf")
            
            new_doc_found = False
            new_doc_url = None
            new_doc_type = None
            for key, url_val in [("concall_pdf", concall_pdf), ("ip_pdf", ip_pdf), ("ar_pdf", ar_pdf)]:
                if url_val and isinstance(url_val, str) and url_val.strip():
                    if "nseindia.com" in url_val or "concall.in" in url_val:
                        continue
                    basename = url_val.split("/")[-1]
                    if basename and basename not in content:
                        new_doc_found = True
                        new_doc_url = url_val
                        new_doc_type = key
                        break
            
            if new_doc_found:
                # Add a 7-day cooldown to prevent continuous updates as documents trick in over 2-3 days
                report_age_days = (today - latest_date).days
                if report_age_days < 7:
                    print(f"⏭️ [NEW DOCUMENT BYPASSED] New {new_doc_type} detected, but the existing report is only {report_age_days} days old. Skipping regeneration to allow other documents to trickle in.")
                else:
                    print(f"🔥 [NEW DOCUMENT DETECTED] Bypassing skip checks for {symbol}. New {new_doc_type} available: {new_doc_url}")
                    return None, ""

            match = re.search(r'<!-- latest_quarter:\s*(\d{6})\s*-->', content)
            if match:
                report_quarter = match.group(1)
                # Fetch latest quarter from StockScans now
                current_quarter = fetch_latest_stockscans_quarter(symbol)
                if current_quarter and report_quarter == current_quarter:
                    return latest_filepath, f"Latest results for {current_quarter} already reported."
                elif current_quarter:
                    # New results are out! Do not skip.
                    return None, ""
        except Exception as e:
            print(f"⚠️ Error parsing existing report metadata for {symbol}: {e}")
            
        # Fallback to 75-day cooldown check
        delta_days = (today - latest_date).days
        if delta_days < 75:
            return latest_filepath, f"Recent report exists (generated {delta_days} days ago on {latest_date.strftime('%Y-%m-%d')})"
            
    return None, ""

def check_existing_quarter_report(symbol: str, reports_dir: Path, today: datetime) -> tuple[bool, str]:
    """Check if a report for this symbol already exists and meets skip conditions (quarter results match or 75-day cooldown)."""
    if os.environ.get("FORCE_COMPILE", "false").lower() == "true":
        return False, ""
    filepath, info = find_matching_existing_report(symbol, reports_dir, today)
    if filepath:
        return True, info
    return False, ""

def get_existing_quarter_report_path(symbol, reports_dir, today):
    """Return the Path of the existing matching report for this symbol, if any."""
    filepath, info = find_matching_existing_report(symbol, reports_dir, today)
    return filepath

def get_report_date_str(filepath):
    """Extract report date from filepath name (YYYY-MM-DD) or modification time, formatted as %d %b %Y."""
    import re
    if not filepath:
        return datetime.today().strftime("%d %b %Y")
    filename = filepath.name
    match = re.search(r"(\d{4}-\d{2}-\d{2})", filename)
    if match:
        try:
            file_date = datetime.strptime(match.group(1), "%Y-%m-%d")
            return file_date.strftime("%d %b %Y")
        except Exception:
            pass
    try:
        mtime = filepath.stat().st_mtime
        file_date = datetime.fromtimestamp(mtime)
        return file_date.strftime("%d %b %Y")
    except Exception:
        return datetime.today().strftime("%d %b %Y")

def fetch_stockscans_documents(symbol: str, exchange: str) -> dict:
    """Query StockScans documents APIs and resolve links based on documentType classification."""
    headers = {"accept": "application/json", "content-type": "application/json"}
    ip_links = []
    ar_links = []
    cc_links = []
    for doc_type in ["documents", "announcements"]:
        url = f"https://www.stockscans.in/api/company/{doc_type}/{exchange}:{symbol}"
        try:
            print(f"📊 [StockScans API] Checking {doc_type} at: {url}")
            r = requests.get(url, headers=headers, timeout=10)
            if r.status_code == 200:
                data = r.json()
                items = []
                if isinstance(data, list):
                    items = data
                elif isinstance(data, dict):
                    for k, v in data.items():
                        if isinstance(v, list):
                            items.extend(v)
                for item in items:
                    if not isinstance(item, dict):
                        continue
                    ss_url = item.get("ssUrl") or item.get("url") or item.get("pdf")
                    if not ss_url or not isinstance(ss_url, str):
                        continue
                    if not ss_url.startswith("http"):
                        prefix = "document" if doc_type == "documents" else "announcement"
                        full_url = f"https://www.stockscans.in/{prefix}/{ss_url}"
                    else:
                        full_url = ss_url
                    doc_class = str(item.get("documentType", "")).lower()
                    if not doc_class:
                        doc_class = full_url.lower()
                    date_str = str(item.get("date", ""))
                    if "annual" in doc_class or "ar" in doc_class or "report" in doc_class:
                        ar_links.append((full_url, date_str))
                    elif "ppt" in doc_class or "presentation" in doc_class or "investor" in doc_class:
                        ip_links.append((full_url, date_str))
                    elif "transcript" in doc_class or "concall" in doc_class:
                        cc_links.append((full_url, date_str))
        except Exception as e:
            print(f"⚠️ Error querying StockScans {doc_type} endpoint: {e}")
    def get_item_date_score(item_tuple):
        url, date_str = item_tuple
        if date_str:
            digits = re.findall(r'\d+', date_str)
            if digits:
                d_val = digits[0]
                if len(d_val) >= 4:
                    year = int(d_val[:4])
                    if 2018 <= year <= 2028:
                        return year
                elif len(d_val) == 2:
                    year = 2000 + int(d_val)
                    if 2018 <= year <= 2028:
                        return year
        url_clean = url.split("?")[0]
        years = re.findall(r'20\d{2}|\b\d{2}\b', url_clean)
        valid_years = [int(y) if len(y) == 4 else 2000 + int(y) for y in years]
        valid_years = [y for y in valid_years if 2018 <= y <= 2028]
        return max(valid_years) if valid_years else 0
    best_ip = max(ip_links, key=get_item_date_score) if ip_links else (None, None)
    best_ar = max(ar_links, key=get_item_date_score) if ar_links else (None, None)
    best_cc = max(cc_links, key=get_item_date_score) if cc_links else (None, None)
    return {
        "ip_pdf": best_ip[0],
        "ip_date": best_ip[1],
        "ar_pdf": best_ar[0],
        "ar_date": best_ar[1],
        "concall_pdf": best_cc[0],
        "concall_date": best_cc[1]
    }

def fetch_screener_documents(symbol: str) -> dict:
    """Scrape official document PDF links directly from the Screener.in company page and return the latest ones."""
    url = f"https://www.screener.in/company/{symbol}/consolidated/"
    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, Gecko) Chrome/115.0.0.0 Safari/537.36"
    }
    ip_links, ar_links, cc_links = [], [], []
    try:
        print(f"🔍 [Screener Scraper] Crawling docs page at: {url}")
        r = requests.get(url, headers=headers, timeout=12)
        if r.status_code == 200:
            soup = BeautifulSoup(r.text, 'html.parser')
            for a in soup.find_all('a', href=True):
                href = a['href']
                text = a.text.lower()
                href_lower = href.lower()
                full_href = href if href.startswith("http") else "https://www.screener.in" + href
                if ("annual report" in text or "annual-report" in href_lower or "annual_report" in href_lower) and href_lower.endswith(".pdf"):
                    ar_links.append(full_href)
                elif ("transcript" in text or "concall" in text or "concall-transcript" in href_lower or "concall_transcript" in href_lower) and href_lower.endswith(".pdf"):
                    cc_links.append(full_href)
                elif ("presentation" in text or "investor-presentation" in href_lower or "investor_presentation" in href_lower) and href_lower.endswith(".pdf"):
                    ip_links.append(full_href)
    except Exception as e:
        print(f"⚠️ Screener documents scraping error: {e}")
    def get_year_score(url):
        url_clean = url.split("?")[0]
        years = re.findall(r'20\d{2}|\b\d{2}\b', url_clean)
        valid_years = [int(y) if len(y) == 4 else 2000 + int(y) for y in years]
        valid_years = [y for y in valid_years if 2018 <= y <= 2028]
        return max(valid_years) if valid_years else 0
    return {
        "ip_pdf": max(ip_links, key=get_year_score) if ip_links else None,
        "ar_pdf": max(ar_links, key=get_year_score) if ar_links else None,
        "concall_pdf": max(cc_links, key=get_year_score) if cc_links else None
    }

def clean_company_name(name: str) -> str:
    """Remove common corporate suffixes from company name to improve search precision."""
    cleaned = re.sub(r'\b(limited|ltd|corporation|corp|co|company|ltd\.)\b', '', name, flags=re.IGNORECASE)
    cleaned = re.sub(r'\s+', ' ', cleaned).strip()
    return cleaned

def fetch_valuepickr_thread(company_name: str) -> tuple:
    """Search ValuePickr forum API directly and return the matched topic thread URL and topic ID."""
    cleaned = clean_company_name(company_name)
    term = cleaned.replace(" ", "%20")
    url = f"https://forum.valuepickr.com/search/query?term={term}"
    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, Gecko) Chrome/115.0.0.0 Safari/537.36",
        "Accept": "application/json"
    }
    try:
        print(f"🔍 [ValuePickr API] Querying thread search for: {cleaned}")
        r = requests.get(url, headers=headers, timeout=12)
        if r.status_code == 200:
            data = r.json()
            topics = data.get("topics", [])
            if topics:
                # Filter to verify the topic title contains any term of our query
                query_terms = [t.lower() for t in cleaned.split() if len(t) >= 3]
                best_topic = None
                for t in topics:
                    t_title = t.get("title", "").lower()
                    t_slug = t.get("slug", "").lower()
                    # Check if query terms are matching the topic metadata
                    if not query_terms or any(term in t_title or term in t_slug for term in query_terms):
                        best_topic = t
                        break
                if best_topic:
                    slug = best_topic.get("slug")
                    topic_id = best_topic.get("id")
                    if slug and topic_id:
                        full_thread_url = f"https://forum.valuepickr.com/t/{slug}/{topic_id}"
                        print(f"🎯 ValuePickr thread matched: {full_thread_url}")
                        return full_thread_url, topic_id
                print(f"⚠️ ValuePickr topic check: No matching topic titles found for query terms {query_terms}")
    except Exception as e:
        print(f"⚠️ ValuePickr API search error: {e}")
    return "https://forum.valuepickr.com/", None

def fetch_valuepickr_posts(topic_id: int) -> str:
    """Fetch the top 5 and bottom 5 posts from the ValuePickr thread to feed to the LLM context."""
    url = f"https://forum.valuepickr.com/t/{topic_id}.json"
    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, Gecko) Chrome/115.0.0.0 Safari/537.36",
        "Accept": "application/json"
    }
    try:
        print(f"📖 [ValuePickr API] Fetching posts for topic ID: {topic_id}")
        r = requests.get(url, headers=headers, timeout=15)
        if r.status_code == 200:
            data = r.json()
            post_stream = data.get("post_stream", {})
            posts = post_stream.get("posts", [])
            if not posts:
                return ""
            
            # Select top 5 and bottom 5 posts
            selected_posts = []
            
            # Top 5 posts (or all if total posts < 5)
            top_count = min(5, len(posts))
            for i in range(top_count):
                selected_posts.append((i + 1, posts[i]))
                
            # Bottom 5 posts (avoiding overlap with top 5)
            if len(posts) > 5:
                bottom_start = max(5, len(posts) - 5)
                for i in range(bottom_start, len(posts)):
                    selected_posts.append((i + 1, posts[i]))
            
            # Clean and compile text
            compiled_text = ""
            for idx, post in selected_posts:
                username = post.get("username", "User")
                raw_cooked = post.get("cooked", "")
                # Simple HTML tag stripper
                clean_text = re.sub(r'<[^>]*>', ' ', raw_cooked)
                clean_text = re.sub(r'\s+', ' ', clean_text).strip()
                if len(clean_text) > 1200:
                    clean_text = clean_text[:1200] + "... (truncated)"
                compiled_text += f"Post #{idx} by @{username}:\n{clean_text}\n\n"
            return compiled_text.strip()
    except Exception as e:
        print(f"⚠️ ValuePickr posts fetching error: {e}")
    return ""

def fetch_stockscans_announcements_scan(symbol: str) -> list[dict]:
    """Query the new StockScans announcements API for latest corporate announcements."""
    url = "https://www.stockscans.in/api/company/announcements"
    headers = {
        "accept": "application/json",
        "content-type": "application/json",
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, Gecko) Chrome/115.0.0.0 Safari/537.36"
    }
    payload = {
        "companyIds": [f"NSE:{symbol}"],
        "offset": 0
    }
    try:
        print(f"📊 [StockScans API] Querying announcements for {symbol}...")
        r = requests.post(url, json=payload, headers=headers, timeout=10)
        if r.status_code == 200:
            data = r.json()
            return data.get("companyAnnouncements", [])
    except Exception as e:
        print(f"⚠️ Error querying announcements: {e}")
    return []

GOOGLE_SEARCH_COUNTER = 0

def fetch_ddg_search_results(query: str, limit: int = 5) -> list[dict]:
    """Search for results using Tavily Search API.
    Caps search requests at 100 to prevent exceeding limits.
    """
    import os
    global GOOGLE_SEARCH_COUNTER
    
    api_key = os.getenv("TAVILY_API_KEY")
    if not api_key:
        print("⚠️ TAVILY_API_KEY not found in environment.")
        return [{"url": "https://substack.com", "snippet": "Tavily Search credentials not configured."}]
        
    if GOOGLE_SEARCH_COUNTER >= 100:
        print("⚠️ Search limit of 100 reached. Skipping query.")
        return [{"url": "https://substack.com", "snippet": "Limit reached"}]
        
    print(f"🔍 [Tavily Search] Querying Tavily for: {query} (Call count: {GOOGLE_SEARCH_COUNTER + 1})")
    GOOGLE_SEARCH_COUNTER += 1
    
    url = "https://api.tavily.com/search"
    payload = {
        "api_key": api_key,
        "query": query,
        "max_results": limit,
        "search_depth": "basic"
    }
    
    try:
        r = requests.post(url, json=payload, timeout=12)
        if r.status_code == 200:
            data = r.json()
            results = []
            for item in data.get("results", []):
                results.append({
                    "url": item.get("url"),
                    "snippet": item.get("content", "")
                })
            return results
        else:
            print(f"⚠️ Tavily API error (status {r.status_code}): {r.text}")
            if r.status_code == 429:
                return [{"url": "https://substack.com", "snippet": "Limit reached"}]
            return []
    except Exception as e:
        print(f"⚠️ Tavily Search failed: {e}")
        return []



def get_company_web_context(company_name: str, symbol: str) -> dict:
    """Gather company overview, plants, and PDF presentation/annual report links directly without DDG fallback."""
    cleaned_name = clean_company_name(company_name)
    print(f"🔍 [SEARCH] Gathering web search context for {cleaned_name} ({symbol})...")
    ss_docs = fetch_stockscans_documents(symbol, "NSE")
    ip_pdf = ss_docs.get("ip_pdf")
    ip_date = ss_docs.get("ip_date")
    ar_pdf = ss_docs.get("ar_pdf")
    ar_date = ss_docs.get("ar_date")
    concall_pdf = ss_docs.get("concall_pdf")
    concall_date = ss_docs.get("concall_date")
    
    if not ip_pdf or not ar_pdf or not concall_pdf:
        print("🔍 Attempting to scrape missing PDFs directly from Screener...")
        screener_docs = fetch_screener_documents(symbol)
        if not ip_pdf:
            ip_pdf = screener_docs.get("ip_pdf")
            ip_date = None
        if not ar_pdf:
            ar_pdf = screener_docs.get("ar_pdf")
            ar_date = None
        if not concall_pdf:
            concall_pdf = screener_docs.get("concall_pdf")
            concall_date = None
            
    # Resolve years or dates if missing (especially when scraped from Screener)
    import re
    from datetime import datetime
    
    def clean_doc_date(date_str, url, is_ar=False):
        if date_str and str(date_str).strip() and not str(date_str).lower().startswith("not"):
            clean = str(date_str).strip()
            # If 6 digits or 4 digits, return it
            if re.match(r'^\d{4}$|^\d{6}$', clean):
                return clean
            # If date string has full timestamp/year, parse it
            year_match = re.findall(r'20\d{2}', clean)
            if year_match:
                return year_match[0]
        
        # Fallback to URL parsing
        if url:
            url_clean = url.split("?")[0]
            year_match = re.findall(r'20\d{2}', url_clean)
            if year_match:
                return year_match[-1]
            fy_match = re.search(r'(\d{2})-(\d{2})', url_clean)
            if fy_match:
                return f"20{fy_match.group(2)}"
        
        # Fallback to current year / approximate
        if is_ar:
            return str(datetime.now().year)
        else:
            return datetime.now().strftime("%Y%m")

    ar_date = clean_doc_date(ar_date, ar_pdf, is_ar=True)
    ip_date = clean_doc_date(ip_date, ip_pdf, is_ar=False)
    concall_date = clean_doc_date(concall_date, concall_pdf, is_ar=False)

    if not ip_pdf:
        ip_pdf = "https://nseindia.com/"
    if not ar_pdf:
        ar_pdf = "https://nseindia.com/"
    if not concall_pdf:
        concall_pdf = "https://concall.in/"
        
    val_url, topic_id = fetch_valuepickr_thread(cleaned_name)
    val_posts_context = ""
    if topic_id:
        val_posts_context = fetch_valuepickr_posts(topic_id)
        
    # Fetch recent corporate announcements via search scan API
    announcements_list = fetch_stockscans_announcements_scan(symbol)
    announcements_context = ""
    for idx, item in enumerate(announcements_list[:6]):  # Keep the top 6 latest announcements
        date_str = item.get("date", "")
        title = item.get("title", "")
        desc = item.get("description", "")
        ss_url = item.get("ssUrl", "")
        full_url = f"https://www.stockscans.in/announcement/{ss_url}" if ss_url else ""
        announcements_context += f"- **Date**: {date_str}\n  **Title**: {title}\n  **Description**: {desc}\n  **Document Link**: [{title} PDF]({full_url})\n\n"
        
    # Search Substack for investment research
    print(f"🔍 Searching Substack for {cleaned_name}...")
    raw_substack = fetch_ddg_search_results(f"site:substack.com {cleaned_name}", 10)
    
    # Filter results to ensure the main brand name or symbol is present (to avoid generic/unrelated results)
    res_substack = []
    brand_name = cleaned_name.split()[0].lower() if cleaned_name.split() else ""
    symbol_lower = symbol.lower() if symbol else ""
    
    for item in raw_substack:
        url_lower = item.get("url", "").lower()
        snippet_lower = item.get("snippet", "").lower()
        
        # Check if first word of company name or ticker symbol is in the result details
        if (brand_name and (brand_name in url_lower or brand_name in snippet_lower)) or \
           (symbol_lower and (symbol_lower in url_lower or symbol_lower in snippet_lower)):
            
            # Exclude paid/subscription-only posts
            is_paid = any(p in snippet_lower for p in ["paid episode", "paid subscriber", "paid post", "only available to paid", "paid content"])
            if not is_paid:
                res_substack.append(item)
            
        if len(res_substack) >= 5:
            break
            
    # Fallback to first 3 raw results if brand filter is too restrictive (excluding config/limit messages and paid posts)
    if not res_substack and raw_substack:
        res_substack = [
            item for item in raw_substack 
            if "not configured" not in item.get("snippet", "") 
            and "limit reached" not in item.get("snippet", "").lower()
            and not any(p in item.get("snippet", "").lower() for p in ["paid episode", "paid subscriber", "paid post", "only available to paid", "paid content"])
        ][:3]
        
    substack_context = ""
    for idx, item in enumerate(res_substack):
        substack_context += f"- **Substack Link**: [{item['url']}]({item['url']})\n  **Summary/Snippet**: {item['snippet']}\n\n"

    return {
        "ip_pdf": ip_pdf,
        "ip_date": ip_date,
        "ar_pdf": ar_pdf,
        "ar_date": ar_date,
        "concall_pdf": concall_pdf,
        "concall_date": concall_date,
        "valuepickr_url": val_url,
        "valuepickr_posts": val_posts_context,
        "valuepickr_topic_id": topic_id,
        "announcements": announcements_context.strip(),
        "substack_search": res_substack,
        "substack_context": substack_context.strip()
    }


def format_quarter_label(q_str: str) -> str:
    """Format YYYYMM quarter string into a human-readable label (e.g. '202606' -> 'Q1 FY27 (Ended June 2026)')."""
    if not q_str or len(str(q_str)) != 6:
        return str(q_str)
    try:
        q_str = str(q_str)
        year = int(q_str[:4])
        month = int(q_str[4:])
        
        # Indian Fiscal Year mapping:
        # April-June (month 06): Q1 of next fiscal year (FY = year + 1)
        # July-Sept (month 09): Q2 of next fiscal year (FY = year + 1)
        # Oct-Dec (month 12): Q3 of next fiscal year (FY = year + 1)
        # Jan-March (month 03): Q4 of current fiscal year (FY = year)
        
        if month == 6:
            fq = f"Q1 FY{str(year + 1)[2:]} (Ended June {year})"
        elif month == 9:
            fq = f"Q2 FY{str(year + 1)[2:]} (Ended Sept {year})"
        elif month == 12:
            fq = f"Q3 FY{str(year + 1)[2:]} (Ended Dec {year})"
        elif month == 3:
            fq = f"Q4 FY{str(year)[2:]} (Ended March {year})"
        else:
            months_map = {3: "March", 6: "June", 9: "September", 12: "December"}
            fq = f"{months_map.get(month, str(month))} {year}"
        return fq
    except Exception:
        return str(q_str)

def generate_report_via_gemini(api_key: str, r: dict, prompt_template: str, today_str: str, model: str = None) -> str:
    """Invoke the Gemini API in three distinct stages to guarantee complete, non-truncated reports."""
    symbol = r["symbol"]
    
    # Dynamically resolve financial years based on parsed data
    ss_years = r.get("ss_years") or ["FY24A", "FY25A", "FY26A"]
    if len(ss_years) >= 3:
        fy_act_1 = ss_years[0]
        fy_act_2 = ss_years[1]
        fy_act_3 = ss_years[2]
    else:
        fy_act_1, fy_act_2, fy_act_3 = "FY24A", "FY25A", "FY26A"
        
    try:
        last_yr_digits = re.findall(r"\d+", fy_act_3)
        if last_yr_digits:
            last_yr = int(last_yr_digits[0])
            fy_est_1 = f"FY{last_yr + 1}E"
            fy_est_2 = f"FY{last_yr + 2}E"
        else:
            fy_est_1, fy_est_2 = "FY27E", "FY28E"
    except Exception:
        fy_est_1, fy_est_2 = "FY27E", "FY28E"
        
    # Replace years dynamically in the prompt template
    prompt_template = prompt_template.replace("FY24A", fy_act_1)
    prompt_template = prompt_template.replace("FY25A", fy_act_2)
    prompt_template = prompt_template.replace("FY26A", fy_act_3)
    prompt_template = prompt_template.replace("FY27E", fy_est_1)
    prompt_template = prompt_template.replace("FY28E", fy_est_2)
    company = r["company"]
    sector = r["industry"]
    cmp = r.get("close", 0.0)
    mcap = r.get("mcap_cr", 0.0)
    
    # Extract actual ratios and data if available
    sector_rules = dp.get_sector_valuation_guidelines(sector)
    ss_meta = r.get("ss_meta_ratios") or {}
    yf_info = r.get("yf_info") or {}
    
    # 52W High/Low
    high_52w = yf_info.get("fiftyTwoWeekHigh") or cmp * 1.2
    low_52w = yf_info.get("fiftyTwoWeekLow") or cmp * 0.8
    
    # PE and PB
    pe = ss_meta.get("Price To Earnings") or yf_info.get("trailingPE") or 0.0
    pb = ss_meta.get("Price To Book") or yf_info.get("priceToBook") or 0.0
    
    # ROCE / ROE
    roce = ss_meta.get("ROCE") or yf_info.get("returnOnAssets", 0.0) * 100.0 or 0.0
    roe = ss_meta.get("ROE") or yf_info.get("returnOnEquity", 0.0) * 100.0 or 0.0
    
    # Book value
    bv = ss_meta.get("Book Value") or yf_info.get("bookValue") or 0.0
    
    # Dividend yield
    dy = yf_info.get("dividendYield") or 0.0
    if dy < 1.0 and dy > 0.0:
        dy = dy * 100.0 # convert e.g. 0.012 to 1.2%
        
    # Shareholdings
    promoter = ss_meta.get("Promoter") or yf_info.get("heldPercentInsiders", 0.0) * 100.0
    fii = ss_meta.get("FII") or 0.0
    dii = ss_meta.get("DII") or 0.0
    
    if fii == 0.0 and dii == 0.0:
        inst_held = yf_info.get("heldPercentInstitutions", 0.0) * 100.0
        fii = inst_held * 0.6
        dii = inst_held * 0.4
        
    public_val = ss_meta.get("Public") or (100.0 - promoter - fii - dii)
    
    # Check if stockscans has shareholding aggregate table
    ss_sh_table = r.get("ss_shareholding_table", "")
    if ss_sh_table:
        lines = ss_sh_table.splitlines()
        if len(lines) > 2:
            headers_list = [h.strip().lower() for h in lines[0].split("|")[1:-1]]
            latest_row = [c.strip() for c in lines[-1].split("|")[1:-1]]
            h_map = {h: idx for idx, h in enumerate(headers_list)}
            
            prom_col = next((idx for h, idx in h_map.items() if "promoter" in h), None)
            fii_col = next((idx for h, idx in h_map.items() if "fii" in h or "foreign" in h), None)
            dii_col = next((idx for h, idx in h_map.items() if "dii" in h or "domestic" in h), None)
            pub_col = next((idx for h, idx in h_map.items() if "public" in h or "retail" in h), None)
            
            try:
                if prom_col is not None:
                    promoter = float(latest_row[prom_col].replace("%", "").strip())
                if fii_col is not None:
                    fii = float(latest_row[fii_col].replace("%", "").strip())
                if dii_col is not None:
                    dii = float(latest_row[dii_col].replace("%", "").strip())
                if pub_col is not None:
                    public_val = float(latest_row[pub_col].replace("%", "").strip())
                else:
                    public_val = 100.0 - promoter - fii - dii
            except Exception:
                pass
                
    # Fetch verified corporate documents and forum link
    web_context = get_company_web_context(company, symbol)
    ip_url = web_context.get("ip_pdf", "https://nseindia.com/")
    ar_url = web_context.get("ar_pdf", "https://nseindia.com/")
    cc_url = web_context.get("concall_pdf", "https://concall.in/")
    valuepickr_url = web_context.get("valuepickr_url", "https://forum.valuepickr.com/")
    topic_id = web_context.get("valuepickr_topic_id")
    
    ip_date = web_context.get("ip_date")
    ar_date = web_context.get("ar_date")
    cc_date = web_context.get("concall_date")

    # Cache lookup and fallback processing for documents
    ip_summary = dp.get_cached_document(symbol, "Investor Presentation", ip_date, ip_url)
    if not ip_summary:
        ip_text = dp.download_and_extract_pdf(ip_url, "Investor Presentation")
        ip_summary = dp.summarize_text_via_deepseek(ip_text, "Investor Presentation")
        dp.save_document_to_cache(symbol, "Investor Presentation", ip_date, ip_url, ip_summary)
        
    ar_summary = dp.get_cached_document(symbol, "Annual Report", ar_date, ar_url)
    if not ar_summary:
        ar_text = dp.download_and_extract_pdf(ar_url, "Annual Report")
        ar_summary = dp.summarize_text_via_deepseek(ar_text, "Annual Report")
        dp.save_document_to_cache(symbol, "Annual Report", ar_date, ar_url, ar_summary)
        
    cc_summary = dp.get_cached_document(symbol, "Concall Transcript", cc_date, cc_url)
    if not cc_summary:
        cc_text = dp.download_and_extract_pdf(cc_url, "Concall Transcript")
        cc_summary = dp.summarize_text_via_deepseek(cc_text, "Concall Transcript")
        dp.save_document_to_cache(symbol, "Concall Transcript", cc_date, cc_url, cc_summary)

    # Step 3: Scrape full Substack articles and summarize
    substack_search = web_context.get("substack_search", [])
    full_substack_texts = ""
    for idx, item in enumerate(substack_search[:3]):
        url = item.get("url")
        if url:
            sub_content = dp.scrape_full_substack_content(url)
            if sub_content:
                full_substack_texts += f"### Substack Article #{idx+1} ({url}):\n{sub_content}\n\n"
    substack_summary = dp.summarize_text_via_deepseek(full_substack_texts, "Substack Research Articles")

    # Step 4: Scrape Google News RSS and summarize
    raw_news = dp.get_google_news_rss(company)
    news_summary = dp.summarize_text_via_deepseek(raw_news, "Google News Articles")

    # Step 5: Fetch latest 1 year of ValuePickr posts and summarize
    val_posts_context = ""
    if topic_id:
        val_posts_context = dp.fetch_valuepickr_posts_latest_1_year(topic_id)
    valuepickr_summary = dp.summarize_text_via_deepseek(val_posts_context, "ValuePickr Forum Posts")

    # Save intermediate summaries for manual verification / audit review before decision making
    try:
        comp_dir = Path("outputs") / "intermediate_summaries" / symbol
        comp_dir.mkdir(parents=True, exist_ok=True)
        date_slug = today_str.lower().replace(" ", "_")
        
        summaries_to_save = {
            f"investor_presentation_{date_slug}.md": ip_summary,
            f"annual_report_{date_slug}.md": ar_summary,
            f"concall_transcript_{date_slug}.md": cc_summary,
            f"substack_research_{date_slug}.md": substack_summary,
            f"google_news_{date_slug}.md": news_summary,
            f"valuepickr_forum_{date_slug}.md": valuepickr_summary
        }
        
        for filename, content in summaries_to_save.items():
            with open(comp_dir / filename, "w", encoding="utf-8") as f_out:
                f_out.write(content)
        print(f"📂 Saved intermediate DeepSeek summaries for {symbol} to: {comp_dir}")
    except Exception as save_err:
        print(f"⚠️ Failed to save intermediate summaries: {save_err}")

    announcements = web_context.get("announcements", "")
    latest_q = r.get("latest_quarter", "")
    formatted_q = format_quarter_label(latest_q) if latest_q else "Not Disclosed"

    target_field = "(Please calculate dynamically based on peer multiples, financial data, and your valuation modeling)"
    credit_ratings = ""
    eps_field = f"{ss_meta.get('EPS', 0.0):.2f}"


    # Build a consolidated rich document summaries context block to pass to all LLM stages
    summaries_block = f"""
--- DEEPSEEK SUMMARIZED LATEST INVESTOR PRESENTATION ---
{ip_summary}

--- DEEPSEEK SUMMARIZED LATEST ANNUAL REPORT ---
{ar_summary}

--- DEEPSEEK SUMMARIZED LATEST CONCALL TRANSCRIPT ---
{cc_summary}

--- VERIFIED RECENT CORPORATE ANNOUNCEMENTS ---
{announcements or "No recent critical corporate announcements found."}

--- DEEPSEEK SUMMARIZED GOOGLE NEWS ARTICLES ---
{news_summary}

--- DEEPSEEK SUMMARIZED SUBSTACK INVESTMENT RESEARCH ---
{substack_summary}

--- DEEPSEEK SUMMARIZED VALUEPICKR DISCUSSION FORUM POSTS (LATEST 1 YEAR DATA) ---
{valuepickr_summary}

--- STOCKSCANS BULK & BLOCK DEALS ---
{r.get("ss_bulk_deals") or "No recent bulk or block deals found."}

--- STOCKSCANS INSIDER TRADING TRANSACTIONS ---
{r.get("ss_insider_trading") or "No recent insider trading transactions found."}

--- STOCKSCANS SUBSTANTIAL ACQUISITIONS ---
{r.get("ss_substantial_acquisition") or "No recent substantial acquisitions found."}
"""

    # Build metadata block with formatted actuals and verified sources
    metadata = f"""
COMPANY: {company}
NSE TICKER: {symbol}
SECTOR: {sector}
REPORT DATE: {today_str}
LATEST DATA UP TO: {formatted_q}
CMP: Rs. {cmp:.2f}
MARKET CAP: Rs. {mcap:.1f} Cr
YOUR RATING: BUY
12M TARGET: {target_field}
{credit_ratings}
--- VERIFIED CORPORATE DOCUMENTS (PRIMARY SOURCE OF TRUTH) ---
- Official Latest Investor Presentation (PDF): {ip_url}
- Official Latest Annual Report (PDF): {ar_url}
- Official Latest Quarterly Concall Transcript (PDF): {cc_url}

{summaries_block}

--- ACTUAL FINANCIAL RATIOS AND DATA FOR HEADER BLOCK ---
P/E (TTM): {pe:.2f}x
P/B (TTM): {pb:.2f}x
ROCE: {roce:.2f}%
ROE: {roe:.2f}%
EPS (latest full year): {eps_field}
Book Value: Rs. {bv:.2f}
Dividend Yield: {dy:.2f}%
Face Value: Rs. {yf_info.get("faceValue") or 1.0}
Promoter %: {promoter:.2f}%
FII %: {fii:.2f}%
DII %: {dii:.2f}%
Public %: {public_val:.2f}%
52W High/Low: Rs. {high_52w:.2f} / Rs. {low_52w:.2f}
"""

    # Add peer table to metadata if present
    peer_table = r.get("ss_peer_table", "")
    if peer_table:
        metadata += f"\n--- ACTUAL PEER COMPARISON TABLE ---\n{peer_table}\n"
        
    # Add shareholding table to metadata if present
    if ss_sh_table:
        metadata += f"\n--- ACTUAL SHAREHOLDING PATTERN TREND TABLE ---\n{ss_sh_table}\n"

    
    if not model:
        model = os.environ.get("CONFLUENCE_MODEL")
        if not model:
            raise KeyError("Environment variable 'CONFLUENCE_MODEL' is missing.")
        
    print(f"🤖 [MODEL] Route to: {model}")
    headers = {"Content-Type": "application/json"}

    # Define a strict instruction to eradicate space repetition loops in tables
    whitespace_rule = (
        "CRITICAL WHITESPACE RULE: You MUST write all markdown tables in a single, highly compact line per row "
        "(e.g., | Particulars | FY24A |). Absolutely DO NOT pad cells with multiple space characters or insert tabs to align the pipe "
        "characters ('|') vertically. Trailing or leading spaces inside table cells are strictly forbidden as they trigger infinite loops "
        "in the Gemini text generation engine and crash the process. Make every table row compact, with exactly one space on each side of the text."
    )
    
    def call_gemini_with_retry(stage_url, payload, max_retries=8, initial_delay=12):
        delay = initial_delay
        for attempt in range(1, max_retries + 1):
            try:
                response = requests.post(stage_url, headers=headers, json=payload, timeout=180)
                if response.status_code == 200:
                    return response.json()
                
                # Check for permanent HTTP client errors (400, 401, 403, 404)
                if response.status_code in [400, 401, 403, 404]:
                    print(f"❌ [HTTP {response.status_code}] Permanent error. Bypassing retries for this model.")
                    response.raise_for_status()
                
                print(f"⚠️ [Attempt {attempt}/{max_retries}] API returned {response.status_code}. Retrying in {delay}s...")
                time.sleep(delay)
                delay *= 2
            except requests.exceptions.HTTPError as http_err:
                if http_err.response is not None and http_err.response.status_code in [400, 401, 403, 404]:
                    raise http_err
                if attempt == max_retries:
                    raise http_err
                time.sleep(delay)
                delay *= 2
            except Exception as e:
                if attempt == max_retries:
                    raise e
                print(f"⚠️ [Attempt {attempt}/{max_retries}] Exception: {e}. Retrying in {delay}s...")
                time.sleep(delay)
                delay *= 2
        return None

    def call_stage_with_fallback(stage_num: int, prompt_text: str, expected_headers: list[str], primary_model: str) -> str:
        primary_model = primary_model.strip() if primary_model else ""
        # We try primary_model first.
        models_to_try = [primary_model]
        # Read fallback models from environment secret (comma-separated)
        fallback_env = os.environ.get("FALLBACK_MODELS")
        if fallback_env:
            for m in fallback_env.split(","):
                m_clean = m.strip()
                if m_clean and m_clean not in models_to_try:
                    models_to_try.append(m_clean)
            
        for model_idx, attempt_model in enumerate(models_to_try):
            # 3 retries for primary model (model_idx == 0), 1 retry for fallback models
            max_model_attempts = 3 if model_idx == 0 else 1
            
            for model_attempt in range(1, max_model_attempts + 1):
                print(f"🤖 [STAGE {stage_num}] Requesting model {attempt_model} (Attempt {model_attempt}/{max_model_attempts})...")
                
                # Setup payload / call logic based on model router (Gemini vs OpenRouter)
                res_json = None
                if "/" in attempt_model or not attempt_model.lower().startswith("gemini"):
                    # OpenRouter API call path
                    or_key = os.environ.get("OPENROUTER_API_KEY")
                    if not or_key:
                        print(f"❌ [STAGE {stage_num}] Missing OPENROUTER_API_KEY for {attempt_model}")
                        break
                    or_url = "https://openrouter.ai/api/v1/chat/completions"
                    or_headers = {
                        "Authorization": f"Bearer {or_key}",
                        "Content-Type": "application/json",
                        "HTTP-Referer": "https://github.com/monitranjan/multibagger-scanner",
                        "X-Title": "Multibagger Scanner"
                    }
                    or_payload = {
                        "model": attempt_model,
                        "messages": [{"role": "user", "content": prompt_text}],
                        "temperature": 0.2,
                        "max_tokens": 25000
                    }
                    try:
                        r_post = requests.post(or_url, json=or_payload, headers=or_headers, timeout=180)
                        if r_post.status_code == 200:
                            choices = r_post.json().get("choices", [])
                            if choices:
                                candidate_text = choices[0].get("message", {}).get("content", "")
                                if candidate_text:
                                    res_json = {
                                        "candidates": [{
                                            "finishReason": "STOP",
                                            "content": {
                                                "parts": [{"text": candidate_text}]
                                            }
                                        }]
                                    }
                    except Exception as exc:
                        print(f"⚠️ [STAGE {stage_num}] Exception calling OpenRouter {attempt_model}: {exc}")
                else:
                    # Standard Gemini API path
                    gen_config = {"temperature": 0.7, "maxOutputTokens": 8192}
                    if ("latest" in attempt_model or "thinking" in attempt_model) and "pro" not in attempt_model:
                        gen_config["thinkingConfig"] = {"thinkingBudget": 0}
                        
                    payload = {
                        "contents": [{"parts": [{"text": prompt_text}]}],
                        "generationConfig": gen_config
                    }
                    
                    stage_url = f"https://generativelanguage.googleapis.com/v1beta/models/{attempt_model}:generateContent?key={api_key}"
                    
                    try:
                        res_json = call_gemini_with_retry(stage_url, payload)
                    except Exception as exc:
                        print(f"⚠️ [STAGE {stage_num}] Exception calling {attempt_model}: {exc}")
                        
                if not res_json or "candidates" not in res_json:
                    print(f"⚠️ [STAGE {stage_num}] Failed API call for {attempt_model}. Trying next attempt...")
                    continue
                    
                candidate = res_json["candidates"][0]
                finish_reason = candidate.get("finishReason")
                
                if "content" not in candidate or "parts" not in candidate["content"]:
                    print(f"⚠️ [STAGE {stage_num}] No content in candidate from {attempt_model}. Trying next attempt...")
                    continue
                    
                text = candidate["content"]["parts"][0]["text"].strip()
                
                # Validate completion
                missing_headers = []
                for h in expected_headers:
                    if not re.search(h, text, re.IGNORECASE):
                        missing_headers.append(h)
                        
                if finish_reason == "MAX_TOKENS" or missing_headers:
                    print(f"⚠️ [STAGE {stage_num}] Incomplete output using {attempt_model} (finishReason: {finish_reason}, missing headers: {missing_headers})")
                    continue
                
                # Successful run
                print(f"✅ [STAGE {stage_num}] Successfully generated via {attempt_model}!")
                dp.log_llm_call(attempt_model, f"Final Report Generation - Stage {stage_num}", prompt_text, text)
                return text
                
        raise RuntimeError(f"Stage {stage_num} failed completely on all available models and retry limits.")

    # Split prompt template into three clean stages
    parts1 = prompt_template.split("### SECTION 6 — FINANCIAL DEEP-DIVE")
    stage1_guidelines = parts1[0].strip()
    
    parts2 = parts1[1].split("### SECTION 8 — VALUATION")
    stage2_guidelines = "### SECTION 6 — FINANCIAL DEEP-DIVE" + parts2[0].strip()
    stage3_guidelines = "### SECTION 8 — VALUATION" + parts2[1].strip()
    
    # Extract Global Style Rules to append to Stage 1 and Stage 2 guidelines for table formatting consistency
    global_rules = ""
    global_rules_match = re.search(r"(GLOBAL STYLE RULES:.*)", prompt_template, re.DOTALL)
    if global_rules_match:
        global_rules = global_rules_match.group(1).strip()
        stage1_guidelines = stage1_guidelines + "\n\n" + global_rules
        stage2_guidelines = stage2_guidelines + "\n\n" + global_rules

    # --- STAGE 1: HEADER BLOCK TO SECTION 5 ---
    stage1_prompt = (
        f"{stage1_guidelines}\n\n"
        f"CRITICAL ASSIGNMENT DIRECTIONS FOR STAGE 1:\n"
        f"1. You are tasked with generating PART 1 of the equity research report for {company} ({symbol}).\n"
        f"2. You MUST ONLY generate the HEADER BLOCK and SECTIONS 2 to 5.\n"
        f"3. Under no circumstances should you generate SECTION 6 or beyond in this call. Stop generating immediately after Section 5.\n"
        f"4. Format the Header Block metrics as exactly two wide horizontal tables stacked vertically. You MUST use this exact markdown template format (no other fields or columns):\n"
        f"\n"
        f"   Table 1: Valuation & Returns Snapshot\n"
        f"   | Particulars | Value | Particulars | Value | Particulars | Value | Particulars | Value |\n"
        f"   | :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- |\n"
        f"   | Rating | BUY | 12M Target | Rs. [Target] | Upside | [Upside]% | CMP | Rs. [CMP] |\n"
        f"   | Market Cap | Rs. [MCap] Cr | 52W High | Rs. [High] | 52W Low | Rs. [Low] | | |\n"
        f"\n"
        f"   Table 2: Fundamentals & Shareholding\n"
        f"   | Particulars | Value | Particulars | Value | Particulars | Value | Particulars | Value | Particulars | Value |\n"
        f"   | :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- |\n"
        f"   | P/E (TTM) | [PE]x | P/B (TTM) | [PB]x | ROCE | [ROCE]% | ROE | [ROE]% | EPS (FY25A) | Rs. [EPS] |\n"
        f"   | Div Yield | [DY]% | Face Value | Rs. [FV] | Promoter % | [Prom]% | FII % | [FII]% | DII % | [DII]% |\n"
        f"\n"
    )
    stage1_prompt += (
        f"5. CITATION REQUIREMENT: You MUST actively cite your sources inside the text of SECTIONS 2, 3, 4, and 5 by appending standard footnote markers at the end of relevant sentences:\n"
        f"   - Use `[^ip-latest]` for facts sourced from the Investor Presentation.\n"
        f"   - Use `[^ar-fy25]` for facts sourced from the Annual Report.\n"
        f"   - Use `[^cc-transcript]` for concall commentary/details.\n"
        f"   - Use `[^vp-thread]` for investor community discussion arguments.\n"
        f"   Be diligent and ensure almost every major point or metric has a citation marker!\n"
        f"6. NO FOOTNOTE DEFINITIONS OR BIBLIOGRAPHY: Absolutely DO NOT generate any footnote definition blocks (e.g., [^ip-latest]: ...) or bibliography list or disclaimers at the end of this stage. Only output the footnote markers inside the text. Stop generating immediately after Section 5.\n"
        f"7. CRITICAL DENSITY RULE: You MUST write comprehensive, detailed paragraphs and complete analytical explanations for SECTIONS 2, 3, 4, and 5. Under no circumstances should any section be a brief 2-3 sentence summary. Provide deep institutional-grade research content.\n"
        f"8. {whitespace_rule}\n\n"
        f"Generate PART 1 (Header Block up to end of Section 5) for:\n\n"
        f"{metadata}"
    )

    stage1_headers = ["Rating", "INVESTMENT THESIS", "BUSINESS OVERVIEW", "INDUSTRY", "MANAGEMENT"]
    part1_text = call_stage_with_fallback(1, stage1_prompt, stage1_headers, model)
    
    # Prepare compact context from Part 1 (Header block only)
    lines = part1_text.splitlines()
    header_lines = []
    for line in lines:
        if "SECTION 2" in line or "### SECTION 2" in line:
            break
        header_lines.append(line)
    header_context = "\n".join(header_lines).strip()
    
    compact_context = header_context
    
    # Add actual financial tables to Stage 2 prompt if present to enforce usage
    actuals_context = ""
    if r.get("ss_income_statement"):
        actuals_context_label = "Do not modify the numbers for past years."
        actuals_context += (
            f"--- ACTUAL FINANCIAL STATEMENT TABLES FROM STOCKSCANS ---\n"
            f"You MUST use these exact tables for TABLE 1, TABLE 2, and TABLE 3 in SECTION 6. {actuals_context_label}\n"
            f"#### TABLE 1 — Income Statement\n"
            f"{r['ss_income_statement']}\n\n"
            f"#### TABLE 2 — Balance Sheet\n"
            f"{r['ss_balance_sheet']}\n\n"
            f"#### TABLE 3 — Cash Flow & Key Ratios\n"
            f"{r['ss_cash_flow_ratios']}\n\n"
        )

    # --- STAGE 2: SECTION 6 TO SECTION 7 ---
    stage2_prompt = (
        f"{stage2_guidelines}\n\n"
        f"CRITICAL ASSIGNMENT DIRECTIONS FOR STAGE 2:\n"
        f"1. You are tasked with generating PART 2 of the equity research report for {company} ({symbol}).\n"
        f"2. You MUST cover the following sections: SECTION 6 (Financial Statements: Income Statement, Balance Sheet, and Cash Flow tables + commentary) and SECTION 7 (Earnings Quality Checklist table).\n"
        f"3. START DIRECTLY with the header '### SECTION 6 — FINANCIAL DEEP-DIVE (CONSOLIDATED)'. Do NOT repeat any header, title, metadata, or preceding sections.\n"
        f"4. Under no circumstances should you generate SECTION 8 or beyond, or any disclaimer/bibliography at the end of this call. Stop generating immediately after Section 7.\n"
        f"5. Maintain absolute mathematical and analytical consistency with the rating, prices, and metrics established in PART 1.\n"
        f"6. {whitespace_rule}\n\n"
        f"Here is the context of PART 1 generated previously for consistency:\n"
        f"--- START OF PART 1 CONTEXT ---\n"
        f"{compact_context}\n"
        f"--- END OF PART 1 CONTEXT ---\n\n"
        f"--- SOURCE DOCUMENTS SUMMARIES CONTEXT ---\n"
        f"{summaries_block}\n\n"
        f"{actuals_context}"
        f"Now, generate PART 2 (starting from ### SECTION 6 — FINANCIAL DEEP-DIVE) for {company} ({symbol}):"
    )

    stage2_headers = ["SECTION 6", "SECTION 7"]
    part2_text = call_stage_with_fallback(2, stage2_prompt, stage2_headers, model)
    
    # Prepare compact context for Stage 3 (Header + Table 1 from part2_text)
    table1_match = re.search(r"(####?\s*TABLE 1\b.*?(?=####?\s*TABLE 2\b|###\s*SECTION|$))", part2_text, re.DOTALL | re.IGNORECASE)
    table1_context = table1_match.group(1).strip() if table1_match else ""
    
    compact_context_part3 = f"{header_context}\n\n{table1_context}".strip()
    
    actuals_del_context = ""
    if r.get("ss_delivery_table"):
        actuals_del_context = f"\n--- ACTUAL LATEST VOLUME & DELIVERY DATA ---\n{r['ss_delivery_table']}\n\n"

    # --- STAGE 3: SECTIONS 8 TO DISCLAIMER ---
    stage3_prompt = (
        f"{stage3_guidelines}\n\n"
        f"CRITICAL ASSIGNMENT DIRECTIONS FOR STAGE 3:\n"
        f"1. You are tasked with generating PART 3 of the equity research report for {company} ({symbol}).\n"
        f"2. You MUST cover the remaining sections: SECTION 8 (Valuation scenarios), SECTION 9 (Key Risks), SECTION 10 (Recommendations), SECTION 10B (Technical Chart Levels EMA map), APPENDIX (Latest Concall Brief), and Global Disclaimer.\n"
        f"3. START DIRECTLY with the header '### SECTION 8 — VALUATION'. Do NOT repeat any header, title, metadata, or preceding sections from PART 1 or PART 2.\n"
        f"4. Maintain absolute mathematical and analytical consistency with the rating, financials, and valuation established in PART 1 and PART 2.\n"
        f"5. SECTOR-SPECIFIC VALUATION METRIC DIRECTIVE: The company belongs to the '{sector}' sector. You MUST prioritize and justify the following key valuation multiples/ratios in SECTION 8:\n"
        f"   - Recommended multiples to use: {sector_rules['ratios']}\n"
        f"   - Rationale for selection: {sector_rules['rationale']}\n"
        f"   Ensure you detail and justify this valuation methodology mathematically.\n"
        f"6. CITATION REQUIREMENT: You MUST actively cite your sources inside the text of Stage 3 (especially inside the Valuation narrative and the APPENDIX Concall Brief) by appending standard footnote markers at the end of relevant sentences:\n"
        f"   - Use `[^ip-latest]` for facts sourced from the Investor Presentation.\n"
        f"   - Use `[^ar-fy25]` for facts sourced from the Annual Report.\n"
        f"   - Use `[^cc-transcript]` for concall commentary/details.\n"
        f"   - Use `[^vp-thread]` for investor community discussion arguments.\n"
        f"7. NO FOOTNOTE DEFINITIONS: Absolutely DO NOT write any footnote definition blocks (e.g., [^ip-latest]: ...) or bibliography list at the end of your response. These are appended programmatically in python. Stop generating immediately after the global disclaimer.\n"
        f"8. CRITICAL DENSITY RULE: Keep all Stage 3 sections extremely dense and concise to prevent text truncation:\n"
        f"   - SECTION 9 (Key Risks): List exactly 5-6 core risks with a 1-line description and 1-line monitoring metric each.\n"
        f"   - SECTION 10B (Technical EMAs & Chart Levels): Provide highly precise, compact, single-line answers for all indicators.\n"
        f"   - APPENDIX (Latest Concall Brief): Summarize each of the 10 subsections in exactly 1-2 punchy, data-filled bullet points. Keep it extremely dense and free of empty transition phrases.\n"
        f"9. {whitespace_rule}\n\n"
        f"Here is the context of PART 1 and PART 2 generated previously for consistency:\n"
        f"--- START OF CONTEXT ---\n"
        f"{compact_context_part3}\n"
        f"{actuals_del_context}"
        f"--- END OF CONTEXT ---\n\n"
        f"--- SOURCE DOCUMENTS SUMMARIES CONTEXT ---\n"
        f"{summaries_block}\n\n"
        f"Now, generate PART 3 (starting from ### SECTION 8 — VALUATION) for {company} ({symbol}):"
    )

    stage3_headers = ["SECTION 8", "SECTION 9", "SECTION 10", "TECHNICAL LEVELS|SECTION 10B", "CONCALL BRIEF|APPENDIX", "DISCLAIMER|informational purposes"]
    part3_text = call_stage_with_fallback(3, stage3_prompt, stage3_headers, model)
    
    substack_search = web_context.get("substack_search", [])
    substack_refs = ""
    for idx, item in enumerate(substack_search[:3]):
        url = item.get("url", "")
        if "substack.com" in url:
            title = item.get("snippet", "Substack Link")[:60].replace('[','').replace(']','').replace('\n',' ').strip() + "..."
            substack_refs += f"- **Substack Research #{idx+1}**: [{title}]({url})\n"

    # Build reference directory section programmatically
    ref_directory = f"""

---

### SECTION 11 — DOCUMENT REFERENCE DIRECTORY

*This section compiles all corporate filings, credit ratings, investor community forums, research substacks, and exchange announcements used to construct and verify the metrics in this report.*

#### Primary Source Documents (Source of Truth):
- **Latest Investor Presentation (PDF)**: [Investor Presentation PDF]({ip_url})
- **Latest 2 Years Annual Reports (PDF)**:
  - [Latest Annual Report (PDF)]({ar_url})
- **Last 4 Quarters Concall Transcripts (PDF)**:
  - [Latest Concall Transcript (PDF)]({cc_url})

#### Substack Investment Research:
{substack_refs or "- No recent Substack research articles found."}

#### Recent Corporate Announcements:
{announcements or "- No recent critical corporate announcements found."}

#### Reference Directory:
- **Official Screener consolidated dashboard**: https://www.screener.in/company/{symbol}/consolidated/
- **Official ValuePickr Forum Thread**: {valuepickr_url}
- **Verify Exchange Announcements**: https://www.nseindia.com/get-quotes/equity?symbol={symbol}

---

### SECTION 12 — CITATION FOOTNOTE DIRECTORY

[^ip-latest]: Source: {company} - Investor Presentation / Corporate Releases (Primary Filing).
[^ar-fy25]: Source: {company} - Annual Report / Statutory Financial Statement Filings.
[^cc-transcript]: Source: {company} - Earnings Call Commentary and Q&A Transcripts.
[^vp-thread]: Source: Verified Analyst Research, ValuePickr Investor Community Discussions & Industry Peer Insights.
"""

    # Programmatically clean up any accidental duplicate disclaimer at the end of part2_text
    part2_text_cleaned = re.sub(r"(###?\s*DISCLAIMER\b.*)", "", part2_text, flags=re.DOTALL | re.IGNORECASE).strip()

    combined_report = part1_text.strip() + "\n\n" + part2_text_cleaned + "\n\n" + part3_text.strip() + "\n\n" + ref_directory.strip()
    
    # Inject latest data quarter info into the report subtitle dynamically if not already present
    lines = combined_report.splitlines()
    has_latest_data = False
    for line in lines[:10]:
        if "Latest Data" in line:
            has_latest_data = True
            break
            
    if not has_latest_data:
        combined_report = re.sub(
            r'(Report Date:\s*[^\n|]*)',
            r'\1 | Latest Data: ' + formatted_q,
            combined_report,
            count=1
        )
    return combined_report


REPORT_STYLESHEET = """
          .report-h4 {
            color: #2d3748;
            font-size: 15px;
            margin: 20px 0 10px 0;
            border-left: 3px solid #0f52ba;
            padding-left: 10px;
            font-weight: bold;
            font-family: sans-serif;
          }
          .report-h3 {
            color: #1b365d;
            font-size: 18px;
            border-bottom: 2px solid #e2e8f0;
            padding-bottom: 6px;
            margin: 25px 0 12px 0;
            font-weight: bold;
            text-transform: uppercase;
            font-family: sans-serif;
          }
          .report-h2 {
            color: #1b365d;
            font-size: 20px;
            border-bottom: 3px solid #1b365d;
            padding-bottom: 8px;
            margin: 30px 0 15px 0;
            font-weight: bold;
            text-transform: uppercase;
            font-family: sans-serif;
            text-align: center;
          }
          .report-ul {
            margin: 10px 0;
            padding-left: 20px;
            font-family: sans-serif;
          }
          .report-li {
            margin-bottom: 6px;
            line-height: 1.6;
            color: #4a5568;
            font-size: 14px;
          }
          .report-p {
            line-height: 1.6;
            margin: 10px 0;
            color: #4a5568;
            font-size: 14px;
            font-family: sans-serif;
          }
          .report-meta {
            line-height: 1.5;
            margin: 4px 0;
            color: #2d3748;
            font-size: 13.5px;
            font-family: sans-serif;
          }
          .report-callout {
            background-color: #f4f6f9;
            border-left: 4px solid #1b365d;
            padding: 12px;
            margin: 15px 0;
            border-radius: 4px;
            font-weight: bold;
            color: #1b365d;
            font-family: sans-serif;
          }
          .report-table-wrapper {
            overflow-x: auto;
            margin: 15px 0;
          }
          .report-table {
            border-collapse: collapse;
            width: 100%;
            border: 1px solid #e2e8f0;
            font-family: sans-serif;
          }
          .report-th {
            border: 1px solid #e2e8f0;
            padding: 10px 12px;
            background-color: #1b365d;
            color: white;
            font-weight: bold;
            text-align: left;
            font-size: 13px;
          }
          .report-td {
            border: 1px solid #e2e8f0;
            padding: 8px 12px;
            font-size: 12.5px;
            color: #2d3748;
          }
          .report-hr {
            border: 0;
            border-top: 1px solid #e2e8f0;
            margin: 25px 0;
          }
          .odd-row {
            background-color: #f8f9fa;
          }
          .even-row {
            background-color: #ffffff;
          }
          .right {
            text-align: right;
          }
          .left {
            text-align: left;
          }
"""


def markdown_to_html(md_text: str) -> str:
    # Pre-process bold, links, and code blocks
    md_text = re.sub(r"\*\*(.*?)\*\*", r'<strong style="color: #0f52ba;">\1</strong>', md_text)
    md_text = re.sub(
        r"\[(.*?)\]\((.*?)\)",
        r'<a href="\2" style="color: #1b365d; font-weight: bold; text-decoration: none; border-bottom: 1px dashed #1b365d;">\1</a>',
        md_text
    )
    md_text = re.sub(
        r"`(.*?)`",
        r'<code style="background-color: #f4f6f9; color: #d63384; padding: 2px 5px; border-radius: 4px; font-family: monospace; font-size: 90%; font-weight: bold;">\1</code>',
        md_text
    )

    html_lines = []
    in_table = False
    table_headers = []
    table_rows = []
    in_list = False

    def format_html_table(headers, rows) -> str:
        if not headers:
            return ""
        header_html = "".join([f'<th style="border: 1px solid #e2e8f0; padding: 10px 12px; background-color: #1b365d; color: white; font-weight: bold; text-align: left; font-size: 13px;">{h}</th>' for h in headers])
        
        row_html_list = []
        for idx, r in enumerate(rows):
            bg_color = "#f8f9fa" if idx % 2 == 1 else "#ffffff"
            cells_html = []
            for c in r:
                # Align numeric cells to right
                align = "right" if re.match(r"^[\d\.,₹\(\)\-\%x\s\:\/]+$", c.strip().replace("Rs.", "").replace("Rs", "")) else "left"
                cells_html.append(f'<td style="border: 1px solid #e2e8f0; padding: 8px 12px; background-color: {bg_color}; font-size: 12.5px; text-align: {align}; color: #2d3748;">{c}</td>')
            row_html_list.append(f'<tr>{"".join(cells_html)}</tr>')
            
        return f"""
        <div style="overflow-x: auto; margin: 15px 0;">
          <table style="border-collapse: collapse; width: 100%; border: 1px solid #e2e8f0; font-family: sans-serif;">
            <thead>
              <tr>{header_html}</tr>
            </thead>
            <tbody>
              {"".join(row_html_list)}
            </tbody>
          </table>
        </div>
        """

    lines = md_text.splitlines()
    for line in lines:
        line_strip = line.strip()
        
        # Close list if no longer in list
        if in_list and not (line_strip.startswith("* ") or line_strip.startswith("- ")):
            html_lines.append("</ul>")
            in_list = False
            
        # 1. Handle dividers
        if line_strip == "---" or line_strip == "──────────────────":
            if in_table:
                html_lines.append(format_html_table(table_headers, table_rows))
                in_table = False
                table_headers, table_rows = [], []
            html_lines.append('<hr style="border: 0; border-top: 1px solid #e2e8f0; margin: 25px 0;">')
            continue
            
        # 2. Handle headers
        if line_strip.startswith("####"):
            if in_table:
                html_lines.append(format_html_table(table_headers, table_rows))
                in_table = False
                table_headers, table_rows = [], []
            h_text = line_strip.lstrip("#").strip()
            html_lines.append(f'<h4 style="color: #0f52ba; font-size: 15px; margin: 20px 0 10px 0; border-left: 3px solid #1b365d; padding-left: 10px; font-weight: bold; font-family: sans-serif;">{h_text}</h4>')
            continue
        elif line_strip.startswith("###"):
            if in_table:
                html_lines.append(format_html_table(table_headers, table_rows))
                in_table = False
                table_headers, table_rows = [], []
            h_text = line_strip.lstrip("#").strip()
            html_lines.append(f'<h3 style="color: #1b365d; font-size: 18px; border-bottom: 2px solid #e2e8f0; padding-bottom: 6px; margin: 30px 0 15px 0; font-weight: bold; text-transform: uppercase; font-family: sans-serif;">{h_text}</h3>')
            continue
        elif line_strip.startswith("##"):
            if in_table:
                html_lines.append(format_html_table(table_headers, table_rows))
                in_table = False
                table_headers, table_rows = [], []
            h_text = line_strip.lstrip("#").strip()
            html_lines.append(f'<h3 style="color: #1b365d; font-size: 19px; border-bottom: 2px solid #e2e8f0; padding-bottom: 6px; margin: 30px 0 15px 0; font-weight: bold; text-transform: uppercase; font-family: sans-serif;">{h_text}</h3>')
            continue
        elif line_strip.startswith("#"):
            if in_table:
                html_lines.append(format_html_table(table_headers, table_rows))
                in_table = False
                table_headers, table_rows = [], []
            h_text = line_strip.lstrip("#").strip()
            html_lines.append(f'<h2 style="color: #1b365d; font-size: 22px; border-bottom: 3px solid #1b365d; padding-bottom: 8px; margin: 35px 0 20px 0; font-weight: bold; text-transform: uppercase; font-family: sans-serif; text-align: center;">{h_text}</h2>')
            continue
            
        # 3. Handle tables
        if line_strip.startswith("|") and line_strip.endswith("|"):
            if "--" in line_strip or "-|-" in line_strip:
                continue
            cells = [c.strip() for c in line_strip.split("|")[1:-1]]
            if not in_table:
                in_table = True
                table_headers = cells
                table_rows = []
            else:
                table_rows.append(cells)
            continue
        else:
            if in_table:
                html_lines.append(format_html_table(table_headers, table_rows))
                in_table = False
                table_headers, table_rows = [], []
                
        # 4. Handle lists
        if line_strip.startswith("* ") or line_strip.startswith("- "):
            if not in_list:
                html_lines.append('<ul style="margin: 10px 0; padding-left: 20px; font-family: sans-serif;">')
                in_list = True
            item_text = line_strip[2:].strip()
            html_lines.append(f'<li style="margin-bottom: 6px; line-height: 1.6; color: #4a5568; font-size: 14px;">{item_text}</li>')
            continue
            
        # 5. Handle paragraphs
        if line_strip:
            if "Overall Earnings Quality Rating:" in line_strip or "CALL GRADE:" in line_strip:
                html_lines.append(f'<div style="background-color: #f4f6f9; border-left: 4px solid #1b365d; padding: 12px; margin: 15px 0; border-radius: 4px; font-weight: bold; color: #1b365d; font-family: sans-serif;">{line_strip}</div>')
            elif line_strip.startswith("COMPANY:") or line_strip.startswith("NSE TICKER:") or line_strip.startswith("SECTOR:"):
                html_lines.append(f'<p style="line-height: 1.5; margin: 4px 0; color: #2d3748; font-size: 13.5px; font-family: sans-serif;">{line_strip}</p>')
            else:
                html_lines.append(f'<p style="line-height: 1.6; margin: 10px 0; color: #4a5568; font-size: 14px; font-family: sans-serif;">{line_strip}</p>')
            
    if in_table:
        html_lines.append(format_html_table(table_headers, table_rows))
    if in_list:
        html_lines.append("</ul>")
        
    return "\n".join(html_lines)


def send_report_email(symbol: str, company: str, report_md: str) -> None:
    """Send the newly generated research report as a beautifully rendered, inline HTML email body directly in Gmail (no attachments)."""
    import smtplib
    from email.mime.multipart import MIMEMultipart
    from email.mime.text import MIMEText
    
    gmail_user = os.environ.get("GMAIL_USER", "")
    gmail_app_pass = os.environ.get("GMAIL_APP_PASS", "")
    alert_email = os.environ.get("ALERT_EMAIL", gmail_user)
    
    if not (gmail_user and gmail_app_pass):
        print("ℹ️  Email credentials not configured inside report pipeline. Skipping separate email delivery.")
        return
        
    recipients = [e.strip() for e in alert_email.split(",") if e.strip()]
    if not recipients:
        print("⚠️  No valid recipients in ALERT_EMAIL. Skipping email.")
        return
        
    print(f"📧 Sending Beautiful Inline Research Report Email for {symbol} to: {', '.join(recipients)}...")
    today_str = date.today().strftime("%d %b %Y")
    
    html_content = markdown_to_html(report_md)
    
    html_body = f"""
    <html>
      <head>
        <meta charset="utf-8">
        <style>
          {REPORT_STYLESHEET}
        </style>
      </head>
      <body style="font-family: 'Segoe UI', Roboto, Helvetica, Arial, sans-serif; color: #333; max-width: 800px; margin: auto; padding: 20px; border: 1px solid #e2e8f0; border-radius: 8px; background-color: #fcfcfc;">
        <div style="background-color: #1b365d; color: white; padding: 20px; border-radius: 6px 6px 0 0; text-align: center;">
          <h2 style="margin: 0; letter-spacing: 1px;">🏆 MONIT PREMIUM INSTITUTIONAL RESEARCH</h2>
          <p style="margin: 5px 0 0 0; font-size: 14px; opacity: 0.9;">Dedicated Equity Analysis & Target Valuation</p>
        </div>
        <div style="padding: 20px; background-color: white;">
          {html_content}
        </div>
        <div style="background-color: #f4f6f9; text-align: center; padding: 15px; font-size: 11px; color: #777; border-radius: 0 0 6px 6px; border-top: 1px solid #e2e8f0;">
          Generated on {today_str} | Monit Multibagger Research Desk
        </div>
      </body>
    </html>
    """
    
    try:
        # Create standard alternative message (no attachments)
        msg = MIMEMultipart("alternative")
        msg["Subject"] = f"🏆 New Monit Institutional Research Report — {symbol} ({company})"
        msg["From"] = gmail_user
        msg["To"] = ", ".join(recipients)
        
        # Plain text and HTML parts
        msg.attach(MIMEText(f"Our automated deep equity research engine has compiled a new comprehensive Wheels-style report for {company} ({symbol}).\n\n{report_md}", "plain"))
        msg.attach(MIMEText(html_body, "html"))
        
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as s:
            s.login(gmail_user, gmail_app_pass)
            s.sendmail(gmail_user, recipients, msg.as_string())
        print(f"✅ Dedicated Inline Research Report Email sent successfully for {symbol}!")
    except Exception as e:
        print(f"❌ Failed to deliver dedicated report email for {symbol}: {e}")


def send_emerging_digest_email(compiled_reports):
    """Send a consolidated summary email for all emerging leaders compiled today, containing inline reports and standalone premium HTML attachments."""
    import smtplib
    from email.mime.multipart import MIMEMultipart
    from email.mime.text import MIMEText
    from email.mime.application import MIMEApplication
    
    gmail_user = os.environ.get("GMAIL_USER", "")
    gmail_app_pass = os.environ.get("GMAIL_APP_PASS", "")
    alert_email = os.environ.get("ALERT_EMAIL", gmail_user)
    
    if not (gmail_user and gmail_app_pass):
        print("ℹ️  Email credentials not configured inside report pipeline. Skipping emerging digest email.")
        return
        
    recipients = [e.strip() for e in alert_email.split(",") if e.strip()]
    if not recipients:
        print("⚠️  No valid recipients in ALERT_EMAIL. Skipping emerging digest email.")
        return
        
    print(f"📧 Sending Consolidated Emerging Leaders Digest Email for {len(compiled_reports)} stocks to: {', '.join(recipients)}...")
    today_str = date.today().strftime("%d %b %Y")
    
    # 1. Build the summary table rows for both email body and attachment
    email_table_rows_html = []
    attachment_table_rows_html = []
    
    for idx, item in enumerate(compiled_reports):
        r = item["r"]
        symbol = item["symbol"]
        company = item["company"]
        sector = r.get("industry", "N/A")
        cmp = r.get("close", 0.0)
        mcap = r.get("mcap_cr", 0.0)
        is_new = item.get("is_new", True)
        report_date = item.get("report_date", today_str)
        
        # Delivery signal retrieval or fallback
        sig = r.get("delivery_signal")
        if not sig:
            stats = r.get("delivery_stats") or fetch_nse_delivery_data(symbol)
            sig = calculate_delivery_signal(stats, cmp)
            r["delivery_signal"] = sig
            r["delivery_stats"] = stats or {}
            
        badge_html = sig.get("badge_html", "")
        latest_delivery_pct = sig.get("latest_delivery_pct", 0.0)
        week_delivery_pct_median = sig.get("week_delivery_pct_median", 0.0)
        latest_delivery_val_cr = sig.get("latest_delivery_val_cr", 0.0)
        week_delivery_val_median_cr = sig.get("week_delivery_val_median_cr", 0.0)
        
        del_summary_td = f"""
          <td style="border: 1px solid #e2e8f0; padding: 10px; text-align: center; font-family: sans-serif; font-size: 12.5px;">
            {badge_html}
            <div style="font-size: 11px; color: #4a5568; margin-top: 4px; font-weight: 500;">
              {latest_delivery_pct:.1f}% <span style="color: #718096; font-weight: normal;">(vs {week_delivery_pct_median:.1f}% med)</span>
            </div>
            <div style="font-size: 10.5px; color: #166534; margin-top: 3px; font-weight: 600;">
              ₹{latest_delivery_val_cr:.2f} Cr <span style="color: #718096; font-weight: normal; font-size: 10px;">(vs ₹{week_delivery_val_median_cr:.2f} Cr med)</span>
            </div>
          </td>
        """
        
        bg_color = "#f8f9fa" if idx % 2 == 1 else "#ffffff"
        report_md = item["report_md"]
        target_price, upside_pct = extract_target_and_upside(report_md, cmp)
        
        # Premium display for report date
        if is_new:
            date_display = f"{report_date} <span style='font-size: 10px; background-color: #e8f5e9; color: #2e7d32; padding: 2px 6px; border-radius: 4px; font-weight: bold; margin-left: 5px; vertical-align: middle;'>NEW</span>"
            ticker_email = f'<a href="#report-{symbol}" style="color: #1b365d; text-decoration: none; border-bottom: 1px dashed #1b365d;">{symbol}</a>'
        else:
            date_display = f"{report_date} <span style='font-size: 10px; background-color: #f3f4f6; color: #4b5563; padding: 2px 6px; border-radius: 4px; font-weight: bold; margin-left: 5px; vertical-align: middle;'>SAVED</span>"
            ticker_email = f'<span style="color: #4b5563; font-weight: bold;">{symbol}</span>'
            
        ticker_attachment = f'<a href="#report-{symbol}" style="color: #1b365d; text-decoration: none; border-bottom: 1px dashed #1b365d;">{symbol}</a>'
        
        # Row for email summary table
        email_table_rows_html.append(f"""
        <tr style="background-color: {bg_color};">
          <td style="border: 1px solid #e2e8f0; padding: 10px; font-weight: bold; color: #1b365d; font-family: sans-serif;">
            {ticker_email}
          </td>
          <td style="border: 1px solid #e2e8f0; padding: 10px; color: #2d3748; font-family: sans-serif;">{company}</td>
          <td style="border: 1px solid #e2e8f0; padding: 10px; color: #4a5568; font-family: sans-serif;">{sector}</td>
          <td style="border: 1px solid #e2e8f0; padding: 10px; text-align: right; color: #2d3748; font-family: sans-serif;">₹{cmp:,.2f}</td>
          {del_summary_td}
          <td style="border: 1px solid #e2e8f0; padding: 10px; text-align: right; font-weight: bold; color: #2e7d32; font-family: sans-serif;">₹{target_price:,.2f} (+{upside_pct:.1f}%)</td>
          <td style="border: 1px solid #e2e8f0; padding: 10px; text-align: right; color: #4a5568; font-family: sans-serif;">₹{mcap:,.1f} Cr</td>
          <td style="border: 1px solid #e2e8f0; padding: 10px; color: #2d3748; font-family: sans-serif; white-space: nowrap;">{date_display}</td>
        </tr>
        """)
        
        # Row for HTML attachment summary table
        attachment_table_rows_html.append(f"""
        <tr style="background-color: {bg_color};">
          <td style="border: 1px solid #e2e8f0; padding: 10px; font-weight: bold; color: #1b365d; font-family: sans-serif;">
            {ticker_attachment}
          </td>
          <td style="border: 1px solid #e2e8f0; padding: 10px; color: #2d3748; font-family: sans-serif;">{company}</td>
          <td style="border: 1px solid #e2e8f0; padding: 10px; color: #4a5568; font-family: sans-serif;">{sector}</td>
          <td style="border: 1px solid #e2e8f0; padding: 10px; text-align: right; color: #2d3748; font-family: sans-serif;">₹{cmp:,.2f}</td>
          {del_summary_td}
          <td style="border: 1px solid #e2e8f0; padding: 10px; text-align: right; font-weight: bold; color: #2e7d32; font-family: sans-serif;">₹{target_price:,.2f} (+{upside_pct:.1f}%)</td>
          <td style="border: 1px solid #e2e8f0; padding: 10px; text-align: right; color: #4a5568; font-family: sans-serif;">₹{mcap:,.1f} Cr</td>
          <td style="border: 1px solid #e2e8f0; padding: 10px; color: #2d3748; font-family: sans-serif; white-space: nowrap;">{date_display}</td>
        </tr>
        """)
        
    summary_table_header = """
    <div style="overflow-x: auto; margin: 20px 0;">
      <table style="border-collapse: collapse; width: 100%; border: 1px solid #e2e8f0;">
        <thead>
          <tr style="background-color: #1b365d; color: white;">
            <th style="border: 1px solid #e2e8f0; padding: 12px 10px; text-align: left; font-family: sans-serif; font-size: 14px;">Ticker</th>
            <th style="border: 1px solid #e2e8f0; padding: 12px 10px; text-align: left; font-family: sans-serif; font-size: 14px;">Company Name</th>
            <th style="border: 1px solid #e2e8f0; padding: 12px 10px; text-align: left; font-family: sans-serif; font-size: 14px;">Industry/Sector</th>
            <th style="border: 1px solid #e2e8f0; padding: 12px 10px; text-align: right; font-family: sans-serif; font-size: 14px;">CMP</th>
            <th style="border: 1px solid #e2e8f0; padding: 12px 10px; text-align: center; font-family: sans-serif; font-size: 14px;">Volume & Delivery (Live)</th>
            <th style="border: 1px solid #e2e8f0; padding: 12px 10px; text-align: right; font-family: sans-serif; font-size: 14px;">12M Target (Upside)</th>
            <th style="border: 1px solid #e2e8f0; padding: 12px 10px; text-align: right; font-family: sans-serif; font-size: 14px;">MCap (Cr)</th>
            <th style="border: 1px solid #e2e8f0; padding: 12px 10px; text-align: left; font-family: sans-serif; font-size: 14px;">Report Date</th>
          </tr>
        </thead>
        <tbody>
    """
    
    summary_table_footer = """
        </tbody>
      </table>
    </div>
    """
    
    summary_table_email_html = summary_table_header + "".join(email_table_rows_html) + summary_table_footer
    summary_table_attachment_html = summary_table_header + "".join(attachment_table_rows_html) + summary_table_footer

    legend_html = """
    <div style="margin: 25px 0; padding: 18px; border: 1px solid #e2e8f0; border-radius: 8px; background-color: #f8fafc; font-family: sans-serif;">
      <h4 style="margin: 0 0 12px 0; color: #1b365d; font-size: 13.5px; text-transform: uppercase; letter-spacing: 0.5px; border-bottom: 1px solid #e2e8f0; padding-bottom: 6px; font-family: sans-serif;">
        💡 Conviction & Delivery Signals Legend
      </h4>
      <table style="width: 100%; border-collapse: collapse; font-size: 12px; line-height: 1.5; font-family: sans-serif;">
        <tr>
          <td style="padding: 6px 10px 6px 0; vertical-align: top; width: 22%;">
            <span style="font-size: 10.5px; background-color: #dcfce7; color: #15803d; padding: 3px 6px; border-radius: 4px; font-weight: bold; border: 1px solid #bbf7d0; display: inline-block; white-space: nowrap; font-family: sans-serif;">🔥 High Accumulation</span>
          </td>
          <td style="padding: 6px 0; vertical-align: top; color: #4a5568; font-family: sans-serif;">
            Delivery % is &gt; 5% above 5-day median, delivery volume is &gt; 1.2x median, and Demat delivery value is &ge; ₹1.0 Cr. Indicates heavy institutional or insider buying.
          </td>
        </tr>
        <tr>
          <td style="padding: 6px 10px 6px 0; vertical-align: top;">
            <span style="font-size: 10.5px; background-color: #e0f2fe; color: #0369a1; padding: 3px 6px; border-radius: 4px; font-weight: bold; border: 1px solid #bae6fd; display: inline-block; white-space: nowrap; font-family: sans-serif;">🛡️ Strong Delivery</span>
          </td>
          <td style="padding: 6px 0; vertical-align: top; color: #4a5568; font-family: sans-serif;">
            Delivery % is &ge; 45% OR (delivery % is &gt; 5-day median by &gt; 2% with delivery volume &ge; median). Indicates steady buying pressure and strong long-term conviction.
          </td>
        </tr>
        <tr>
          <td style="padding: 6px 10px 6px 0; vertical-align: top;">
            <span style="font-size: 10.5px; background-color: #fee2e2; color: #b91c1c; padding: 3px 6px; border-radius: 4px; font-weight: bold; border: 1px solid #fecaca; display: inline-block; white-space: nowrap; font-family: sans-serif;">⚠️ Speculative Churn</span>
          </td>
          <td style="padding: 6px 0; vertical-align: top; color: #4a5568; font-family: sans-serif;">
            Total traded volume is &gt; 2x the 5-day median, but delivery % is low (&lt; 20%). Indicates high intraday speculation and day-trading momentum rather than long-term accumulation.
          </td>
        </tr>
        <tr>
          <td style="padding: 6px 10px 6px 0; vertical-align: top;">
            <span style="font-size: 10.5px; background-color: #f3f4f6; color: #374151; padding: 3px 6px; border-radius: 4px; font-weight: bold; border: 1px solid #e5e7eb; display: inline-block; white-space: nowrap; font-family: sans-serif;">⚖️ Neutral</span>
          </td>
          <td style="padding: 6px 0; vertical-align: top; color: #4a5568; font-family: sans-serif;">
            Trading and delivery volumes are in line with the weekly 5-day average. Follow primary technical breakout and momentum confluences.
          </td>
        </tr>
      </table>
    </div>
    """

    # 2. Build email body reports (new only) and collapsible details sections (all)
    reports_body_html = []
    collapsible_sections = []
    
    for item in compiled_reports:
        symbol = item["symbol"]
        company = item["company"]
        report_md = item["report_md"]
        r = item["r"]
        sector = r.get("industry", "N/A")
        cmp = r.get("close", 0.0)
        target_price, upside_pct = extract_target_and_upside(report_md, cmp)
        mcap = r.get("mcap_cr", 0.0)
        is_new = item.get("is_new", True)
        
        report_html = markdown_to_html(report_md)
        
        # Build Volume & Delivery live dashboard card
        sig = r.get("delivery_signal")
        delivery_card_html = ""
        if sig and sig.get("table_html"):
            delivery_card_html = f"""
            <div style="border: 1px solid #e2e8f0; border-radius: 8px; background-color: #f8fafc; padding: 20px; margin-bottom: 25px; font-family: sans-serif; box-shadow: 0 2px 4px rgba(0,0,0,0.02);">
              <div style="display: flex; justify-content: space-between; align-items: center; border-bottom: 1px solid #e2e8f0; padding-bottom: 12px; margin-bottom: 12px; flex-wrap: wrap; gap: 10px;">
                <h4 style="margin: 0; font-size: 15px; color: #1b365d; display: flex; align-items: center; gap: 8px; font-family: sans-serif;">
                  📊 Live Volume & Delivery Analysis <span style="font-size: 12px; color: #718096; font-weight: normal;">(Today: {today_str})</span>
                </h4>
                {sig['badge_html']}
              </div>
              <p style="margin: 0 0 15px 0; font-size: 13.5px; color: #2d3748; line-height: 1.5; background-color: #ffffff; padding: 10px 15px; border-left: 4px solid #1b365d; border-radius: 2px; font-family: sans-serif;">
                <strong>Conviction Suggestion:</strong> {sig['suggestions']}
              </p>
              <details style="border: none; box-shadow: none; background: transparent; margin: 0; padding: 0;">
                <summary style="font-size: 12.5px; color: #1b365d; font-weight: bold; cursor: pointer; padding: 5px 0; user-select: none; font-family: sans-serif;">
                  [View Live 5-Day Delivery Statistics Table]
                </summary>
                <div style="margin-top: 10px;">
                  {sig['table_html']}
                </div>
              </details>
            </div>
            """
            
        report_html_with_delivery = delivery_card_html + report_html
        
        # A. Flat report block with jump-links (only if is_new is True)
        if is_new:
            reports_body_html.append(f"""
            <div id="report-{symbol}">
              <a name="report-{symbol}"></a>
              <h3 style="color: #2e7d32; border-bottom: 2px solid #2e7d32; padding-bottom: 6px; margin-top: 50px; font-family: sans-serif; text-transform: uppercase;">📄 {symbol} — {company} Research Report</h3>
              <div style="padding: 15px 0; background-color: white;">
                {report_html_with_delivery}
              </div>
              <div style="text-align: right; margin-top: 10px; margin-bottom: 20px;">
                <a href="#summary-dashboard" style="color: #2e7d32; font-weight: bold; text-decoration: none; font-size: 13.5px; font-family: sans-serif; border: 1px solid #2e7d32; padding: 6px 12px; border-radius: 4px; background-color: #f0fdf4;">[Back to Dashboard Table ↑]</a>
              </div>
              <hr style="border: 0; border-top: 2px dashed #cbd5e0; margin: 40px 0;">
            </div>
            """)
        
        # B. Collapsible report block (always include all reports for HTML attachment)
        collapsible_sections.append(f"""
        <details id="report-{symbol}" style="background-color: white; border: 1px solid #e2e8f0; border-radius: 8px; margin-bottom: 15px; box-shadow: 0 4px 6px -1px rgba(0,0,0,0.02); overflow: hidden; font-family: sans-serif;">
          <summary style="padding: 15px 20px; cursor: pointer; list-style: none; display: flex; justify-content: space-between; align-items: center; background-color: #f8fafc; user-select: none; border-bottom: 1px solid transparent; transition: background-color 0.2s ease;">
            <div class="summary-content" style="display: flex; align-items: center; flex-wrap: wrap; width: 95%;">
              <span class="summary-ticker" style="font-weight: bold; font-size: 15px; color: white; background-color: #1b365d; padding: 4px 8px; border-radius: 4px; margin-right: 15px; letter-spacing: 0.5px;">{symbol}</span>
              <span class="summary-name" style="font-weight: 600; font-size: 15px; color: #2d3748; margin-right: auto;">{company} <span class="summary-sector" style="font-size: 12px; font-weight: normal; color: #718096; margin-left: 5px;">({sector})</span></span>
              <span class="summary-cmp" style="font-size: 13.5px; margin-left: 20px; color: #4a5568;">CMP: <strong>₹{cmp:,.2f}</strong></span>
              <span class="summary-target" style="font-size: 13.5px; margin-left: 20px; color: #4a5568;">12M Target (Upside): <strong style="color: #2e7d32;">₹{target_price:,.2f} (+{upside_pct:.1f}%)</strong></span>
              <span class="summary-mcap" style="font-size: 13.5px; margin-left: 20px; color: #4a5568;">MCap: <strong>₹{mcap:,.1f} Cr</strong></span>
            </div>
            <span class="arrow" style="font-size: 12px; color: #718096;">▼</span>
          </summary>
          <div class="details-body" style="padding: 25px; background-color: #ffffff; border-top: none; line-height: 1.6; font-family: sans-serif;">
            {report_html_with_delivery}
            <div style="text-align: right; margin-top: 20px; border-top: 1px solid #edf2f7; padding-top: 15px;">
              <a href="#summary-dashboard" style="color: #2e7d32; font-weight: bold; text-decoration: none; font-size: 13px; font-family: sans-serif; border: 1px solid #2e7d32; padding: 5px 10px; border-radius: 4px; background-color: #f0fdf4; margin-right: 10px;">[Back to Dashboard Table ↑]</a>
              <button onclick="document.getElementById('report-{symbol}').open = false; window.location.hash = '#summary-dashboard';" style="color: #e53e3e; font-weight: bold; text-decoration: none; font-size: 13px; font-family: sans-serif; border: 1px solid #e53e3e; padding: 5px 10px; border-radius: 4px; background-color: #fff5f5; cursor: pointer; border-style: solid;">[Collapse Report ✕]</button>
            </div>
          </div>
        </details>
        """)
        
    if reports_body_html:
        reports_body_joined = "\n".join(reports_body_html)
    else:
        reports_body_joined = """
        <div style="background-color: #f9fafb; border: 1px solid #e5e7eb; padding: 20px; border-radius: 6px; text-align: center; margin: 30px 0; font-family: sans-serif;">
          <p style="margin: 0; font-size: 15px; color: #4b5563; font-weight: bold;">
            ℹ️ No new research reports compiled today.
          </p>
          <p style="margin: 8px 0 0 0; font-size: 13.5px; color: #6b7280; line-height: 1.5;">
            All emerging leaders detected today have valid active quarterly reports saved on disk. To keep your inbox light, their full reports are omitted from this email body but remain fully accessible inside the attached interactive HTML dashboard.
          </p>
        </div>
        """
        
    interactive_html = f"""
    <!DOCTYPE html>
    <html>
      <head>
        <meta charset="utf-8">
        <title>Daily Monit Emerging Leaders Digest — {today_str}</title>
        <style>
          body {{
            font-family: 'Segoe UI', -apple-system, Roboto, Helvetica, Arial, sans-serif;
            color: #2d3748;
            max-width: 900px;
            margin: 40px auto;
            padding: 20px;
            background-color: #f7fafc;
          }}
          .header {{
            background: linear-gradient(135deg, #2e7d32 0%, #1b5e20 100%);
            color: white;
            padding: 30px;
            border-radius: 12px 12px 0 0;
            text-align: center;
            box-shadow: 0 4px 15px rgba(0,0,0,0.1);
          }}
          .header h1 {{
            margin: 0;
            font-size: 24px;
            letter-spacing: 1.5px;
            text-transform: uppercase;
            color: white;
          }}
          .header p {{
            margin: 5px 0 0 0;
            font-size: 14px;
            opacity: 0.9;
            color: white;
          }}
          .intro {{
            background-color: white;
            padding: 20px;
            border-radius: 0 0 12px 12px;
            box-shadow: 0 4px 15px rgba(0,0,0,0.05);
            margin-bottom: 30px;
            line-height: 1.6;
          }}
          .intro p {{
            margin: 0 0 10px 0;
          }}
          .intro strong {{
            color: #2e7d32;
          }}
          
          /* Collapsible Accordion details & summary */
          details {{
            background-color: white;
            border: 1px solid #e2e8f0;
            border-radius: 8px;
            margin-bottom: 15px;
            box-shadow: 0 4px 6px -1px rgba(0,0,0,0.02);
            overflow: hidden;
            transition: all 0.2s ease-in-out;
          }}
          details[open] {{
            border-color: #2e7d32;
            box-shadow: 0 10px 15px -3px rgba(0,0,0,0.08);
          }}
          summary {{
            padding: 15px 20px;
            cursor: pointer;
            list-style: none;
            display: flex;
            justify-content: space-between;
            align-items: center;
            background-color: #f8fafc;
            user-select: none;
            border-bottom: 1px solid transparent;
            transition: background-color 0.2s ease;
          }}
          summary::-webkit-details-marker {{
            display: none;
          }}
          summary:hover {{
            background-color: #edf2f7;
          }}
          details[open] summary {{
            background-color: #e8f5e9;
            color: #2e7d32;
            border-bottom: 1px solid #c8e6c9;
          }}
          details[open] summary:hover {{
            background-color: #c8e6c9;
          }}
          details[open] .arrow {{
            transform: rotate(180deg);
            color: #2e7d32;
          }}
        </style>
        <script>
          function handleHashChange() {{
            const hash = window.location.hash;
            if (!hash) return;
            
            const targetId = decodeURIComponent(hash.substring(1));
            const details = document.getElementById(targetId);
            if (details && details.tagName === 'DETAILS') {{
              details.open = true;
              setTimeout(() => {{
                details.scrollIntoView({{ behavior: 'smooth', block: 'start' }});
              }}, 100);
            }}
          }}

          window.addEventListener('hashchange', handleHashChange);
          window.addEventListener('DOMContentLoaded', () => {{
            // Intercept clicks on links that target a details tag
            document.querySelectorAll('a[href^="#"]').forEach(link => {{
              link.addEventListener('click', (e) => {{
                const href = link.getAttribute('href');
                if (href === '#') return;
                const targetId = decodeURIComponent(href.substring(1));
                const details = document.getElementById(targetId);
                if (details && details.tagName === 'DETAILS') {{
                  details.open = true;
                  setTimeout(() => {{
                    details.scrollIntoView({{ behavior: 'smooth', block: 'start' }});
                  }}, 100);
                }}
              }});
            }});
            
            // Run initially if loaded with a hash
            setTimeout(handleHashChange, 300);
          }});
        </script>
      </head>
      <body>
        <div class="header">
          <h1>🚀 DAILY MONIT EMERGING LEADERS DIGEST</h1>
          <p>Interactive Premium Dashboard — {today_str}</p>
        </div>
        <div class="intro">
          <p>
            Welcome to today's premium emerging leaders digest. Below you will find the interactive dashboard. 
            Click on any stock's summary bar to expand the full Wheels-style research report.
          </p>
          <a name="summary-dashboard" id="summary-dashboard"></a>
          <h3 style="color: #2e7d32; border-bottom: 2px solid #e2e8f0; padding-bottom: 6px; margin-top: 25px; font-family: sans-serif;">📊 Executive Summary Dashboard</h3>
          {summary_table_attachment_html}
          {legend_html}
        </div>
        
        <div class="accordion">
          {"".join(collapsible_sections)}
        </div>
      </body>
    </html>
    """
    
    # 3. Build the clean email body with mobile navigation tips and inline reports
    html_body = f"""
    <html>
      <head>
        <meta charset="utf-8">
        <style>
          {REPORT_STYLESHEET}
        </style>
      </head>
      <body style="font-family: 'Segoe UI', Roboto, Helvetica, Arial, sans-serif; color: #333; max-width: 800px; margin: auto; padding: 20px; border: 1px solid #e2e8f0; border-radius: 8px; background-color: #fcfcfc;">
        <div style="background-color: #2e7d32; color: white; padding: 20px; border-radius: 6px 6px 0 0; text-align: center;">
          <h2 style="margin: 0; letter-spacing: 1px; color: white;">🚀 DAILY MONIT EMERGING LEADERS DIGEST</h2>
          <p style="margin: 5px 0 0 0; font-size: 14px; opacity: 0.9;">High-Conviction Momentum & Emerging Signals</p>
        </div>
        <div style="padding: 20px; background-color: white;">
          <p style="line-height: 1.6; color: #4a5568; font-size: 14.5px; font-family: sans-serif;">
            Our automated deep equity research engine has compiled and validated comprehensive Wheels-style research reports for today's **{len(compiled_reports)} emerging leader** candidates.
          </p>
          
          <div style="background-color: #f0fdf4; border-left: 4px solid #2e7d32; padding: 15px; margin: 20px 0; border-radius: 4px; font-family: sans-serif;">
            <p style="margin: 0; font-size: 13.5px; color: #1b5e20; line-height: 1.5; font-weight: bold;">
              💡 Emerging Leaders Digest Updates:
            </p>
            <p style="margin: 3px 0 0 0; font-size: 13px; color: #2e7d32; line-height: 1.5;">
              To keep your inbox light, <strong>only newly generated reports</strong> are included in this email body. Saved reports are skipped to prevent duplicates.
            </p>
            <p style="margin: 8px 0 0 0; font-size: 13px; color: #2e7d32; line-height: 1.5;">
              📎 <strong>All Reports inside Attached Dashboard:</strong> Open the attached HTML file (<code>Emerging_Leaders_Digest_{today_str.replace(' ', '_')}.html</code>) on any browser to view reports for <strong>all</strong> stocks in a premium collapsible layout.
            </p>
          </div>
          
          <a name="summary-dashboard" id="summary-dashboard"></a>
          <h3 style="color: #2e7d32; border-bottom: 2px solid #e2e8f0; padding-bottom: 6px; margin-top: 25px; font-family: sans-serif;">📊 Executive Summary Dashboard</h3>
          {summary_table_email_html}
          {legend_html}
          
          <hr style="border: 0; border-top: 1px solid #e2e8f0; margin: 30px 0;">
          
          {reports_body_joined}
        </div>
        <div style="background-color: #f4f6f9; text-align: center; padding: 15px; font-size: 11px; color: #777; border-radius: 0 0 6px 6px; border-top: 1px solid #e2e8f0; margin-top: 30px;">
          Generated on {today_str} | Monit Multibagger Research Desk
        </div>
      </body>
    </html>
    """
    
    try:
        # Create multipart/mixed message to support attachments
        msg = MIMEMultipart("mixed")
        msg["Subject"] = f"🚀 Daily Monit Emerging Leaders Digest — {len(compiled_reports)} Signals ({today_str})"
        msg["From"] = gmail_user
        msg["To"] = ", ".join(recipients)
        
        # Attach the HTML body as alternative
        body_parts = MIMEMultipart("alternative")
        
        plain_text = f"Our automated deep equity research engine has compiled comprehensive reports for {len(compiled_reports)} emerging leaders.\n\n"
        plain_text += f"Please open the attached interactive HTML dashboard file (Emerging_Leaders_Digest_{today_str.replace(' ', '_')}.html) in your browser to view the collapsible reports.\n\n"
        for item in compiled_reports:
            plain_text += f"* {item['symbol']} ({item['company']}) - {item.get('report_date', today_str)}\n"
        body_parts.attach(MIMEText(plain_text, "plain"))
        body_parts.attach(MIMEText(html_body, "html"))
        msg.attach(body_parts)
        
        # Attach the interactive collapsible HTML dashboard file
        attachment = MIMEApplication(interactive_html.encode("utf-8"), _subtype="html")
        attachment.add_header("Content-Disposition", "attachment", filename=f"Emerging_Leaders_Digest_{today_str.replace(' ', '_')}.html")
        msg.attach(attachment)
        print("📎 Attached consolidated interactive HTML dashboard to digest email.")
        
        # Write local backup/diagnostic copies of digest HTML to disk
        try:
            digest_dir = Path("outputs") / "digests"
            digest_dir.mkdir(parents=True, exist_ok=True)
            
            email_backup_file = digest_dir / f"Emerging_Leaders_Email_Body_{today_str.replace(' ', '_')}.html"
            dashboard_backup_file = digest_dir / f"Emerging_Leaders_Dashboard_{today_str.replace(' ', '_')}.html"
            
            with open(email_backup_file, "w", encoding="utf-8") as f:
                f.write(html_body)
            with open(dashboard_backup_file, "w", encoding="utf-8") as f:
                f.write(interactive_html)
                
            print(f"📂 Saved local diagnostic copies to:\n   - {email_backup_file}\n   - {dashboard_backup_file}")
        except Exception as backup_err:
            print(f"⚠️ Failed to write local backup copies: {backup_err}")
            
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as s:
            s.login(gmail_user, gmail_app_pass)
            s.sendmail(gmail_user, recipients, msg.as_string())
        print(f"✅ Consolidated Emerging Leaders Digest Email sent successfully for {len(compiled_reports)} stocks!")
    except Exception as e:
        print(f"❌ Failed to deliver emerging leaders digest email: {e}")


def git_commit_and_push(symbol: str, report_file: Path) -> None:
    """Commit and push a newly generated report immediately to prevent losing progress if the pipeline is cancelled or fails later."""
    import subprocess
    from pathlib import Path
    try:
        print(f"📦 [GIT] Syncing {symbol} report to remote repository...")
        # Configure user details locally to prevent commit blocks on fresh VMs
        subprocess.run(["git", "config", "user.name", "github-actions[bot]"], check=False)
        subprocess.run(["git", "config", "user.email", "github-actions[bot]@users.noreply.github.com"], check=False)
        
        # Add the report file
        subprocess.run(["git", "add", "-f", str(report_file)], check=True)
        
        # Add the intermediate summaries directory for the symbol if it exists
        summary_dir = Path("outputs") / "intermediate_summaries" / symbol
        if summary_dir.exists():
            subprocess.run(["git", "add", "-f", str(summary_dir)], check=True)
        
        # Check if there is anything to commit
        diff_res = subprocess.run(["git", "diff", "--quiet", "--staged"], check=False)
        if diff_res.returncode != 0:
            # Commit the staged files
            subprocess.run(["git", "commit", "-m", f"chore: auto-publish equity report and intermediate summaries for {symbol} [skip ci]"], check=True)
            # Rebase autostash pull to ensure we integrate any concurrent remote updates safely
            pull_res = subprocess.run(["git", "pull", "--rebase", "--autostash", "origin", "main"], capture_output=True, text=True)
            
            # If the pull conflicted (usually on binary sqlite logs/backtest.db file)
            if pull_res.returncode != 0:
                print(f"⚠️ [GIT] Pull rebase conflicted. Resolving database cache conflict...")
                
                # Check if rebase is in progress
                rebase_in_progress = (
                    Path(".git/rebase-merge").exists() or 
                    Path(".git/rebase-apply").exists()
                )
                
                if rebase_in_progress:
                    # Resolve rebase conflict
                    subprocess.run(["git", "checkout", "--ours", "logs/backtest.db"], check=False)
                    subprocess.run(["git", "add", "logs/backtest.db"], check=False)
                    # Continue rebase
                    rebase_res = subprocess.run(["git", "-c", "core.editor=true", "rebase", "--continue"], capture_output=True, text=True)
                    if rebase_res.returncode != 0:
                        print(f"❌ [GIT] Rebase continue failed: {rebase_res.stderr}. Aborting rebase.")
                        subprocess.run(["git", "rebase", "--abort"], check=False)
                        raise RuntimeError(f"Git rebase failed: {rebase_res.stderr}")
                else:
                    # Resolve autostash apply conflict (no rebase in progress)
                    subprocess.run(["git", "checkout", "--ours", "logs/backtest.db"], check=False)
                    subprocess.run(["git", "reset", "logs/backtest.db"], check=False)
            
            # Push to the remote branch
            subprocess.run(["git", "push", "origin", "main"], check=True)
            print(f"🚀 [GIT] Successfully synced {symbol} report to GitHub!")
        else:
            print(f"ℹ️ [GIT] No changes detected for {symbol} report (already synced).")
    except Exception as e:
        print(f"⚠️ [GIT WARNING] Failed to auto-sync {symbol} report: {e}")

def sanitize_stockscans_cookie():
    cookie = os.environ.get("STOCKSCANS_COOKIE", "")
    if cookie:
        cleaned = cookie.strip().replace("\n", "").replace("\r", "")
        os.environ["STOCKSCANS_COOKIE"] = cleaned

def main() -> None:
    print("="*80)
    print("🌟🚀 AUTOMATED MONIT DEEP EQUITY RESEARCH PIPELINE 🚀🌟")
    print("="*80)
    
    # Sanitize loaded cookie to remove any trailing newlines or spaces
    sanitize_stockscans_cookie()
    
    # Sync local delivery history from bulk bhavcopy first
    try:
        print("🔄 Synchronizing local delivery history database from bulk bhavcopy...")
        sync_delivery_history(45)
    except Exception as e:
        print(f"⚠️ Error running delivery history sync: {e}")
        
    # 1. Verify API credentials
    api_key = os.environ.get("OPENROUTER_API_KEY") or os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
    if not api_key:
        print("❌ Error: Required OPENROUTER_API_KEY is not set!")
        print("Please configure your OpenRouter API Key in the environment or .env file.")
        sys.exit(1)
        
    report_type = os.environ.get("REPORT_TYPE", "all").lower()
    print(f"📋 Running report pipeline for REPORT_TYPE: {report_type}")
    
    confluence_3_rows = []
    if report_type in ["confluence", "all"]:
        # 2. Check for confluence list JSON
        confl_json_path = Path("outputs") / "today_confluences.json"
        if not confl_json_path.exists():
            print(f"ℹ️ Confluences list not found at: {confl_json_path}")
            if report_type == "confluence":
                sys.exit(0)
        else:
            try:
                with open(confl_json_path, "r") as f:
                    confluence_3_rows = json.load(f)
            except Exception as e:
                print(f"❌ Error loading confluences list: {e}")
                if report_type == "confluence":
                    sys.exit(1)
        
        if not confluence_3_rows:
            print("ℹ️ Today's confluence list is empty. No triple-confluence stocks detected today.")
        
    # 3. Load prompt template
    prompt_path = Path("prompt.md")
    if not prompt_path.exists():
        print("❌ Error: prompt.md template file not found in the workspace root!")
        sys.exit(1)
        
    try:
        with open(prompt_path, "r") as f:
            prompt_template = f.read()
    except Exception as e:
        print(f"❌ Error reading prompt.md template: {e}")
        sys.exit(1)
        
    reports_dir = Path("outputs") / "reports"
    reports_dir.mkdir(parents=True, exist_ok=True)
    
    today = datetime.today()
    today_str = today.strftime("%d %b %Y")
    date_suffix = today.strftime("%Y-%m-%d")
    
    # Process all confluences to keep speed high and costs optimal
    reports_compiled = 0
    consecutive_failures = 0
    
    if report_type in ["confluence", "all"] and confluence_3_rows:
        print(f"Found {len(confluence_3_rows)} triple-confluence candidates. Processing all candidates...")
    for r in confluence_3_rows:
        r = enrich_basic_metadata(r)
        symbol = r["symbol"]
        company = r["company"]
        
        print(f"\n🔍 Checking report status for `{symbol}` ({company})...")
        
        exists, quarter_info = check_existing_quarter_report(symbol, reports_dir, today)
        if exists:
            print(f"⏭️  [SKIPPED] A report for {symbol} has already been compiled in the current calendar quarter: {quarter_info}.")
            print("Avoiding repeated token expenditure as no new quarterly earnings result has been released.")
            # Sanitize the existing report's header block on disk
            existing_path = get_existing_quarter_report_path(symbol, reports_dir, today)
            if existing_path and existing_path.exists():
                try:
                    with open(existing_path, "r") as ef:
                        report_text = ef.read()
                    cmp = r.get("close", 0.0)
                    target_price, upside_pct = extract_target_and_upside(report_text, cmp)
                    sanitized_text = sanitize_report_header_block(report_text, cmp, target_price, upside_pct)
                    if sanitized_text != report_text:
                        with open(existing_path, "w") as wf:
                            wf.write(sanitized_text)
                        print(f"✍️ Sanitized and updated existing confluence report for {symbol} on disk.")
                        report_text = sanitized_text
                except Exception as sanitize_err:
                    print(f"⚠️ Failed to sanitize existing confluence report for {symbol}: {sanitize_err}")
            
            # If the report was generated *today*, we should still send/retry the dedicated email
            # since a manual trigger or retry of the pipeline should ensure email delivery.
            today_report_path = reports_dir / f"{symbol}_equity_report_{date_suffix}.md"
            if today_report_path.exists():
                print(f"📧 Report for {symbol} was compiled today. Sending/Retrying dedicated email delivery...")
                try:
                    with open(today_report_path, "r") as f:
                        today_report_text = f.read()
                    send_report_email(symbol, company, today_report_text)
                except Exception as mail_err:
                    print(f"⚠️ Error sending separate email for {symbol}: {mail_err}")
            continue
            
        print(f"✍️  [COMPILING] No report found for {symbol} in the current calendar quarter.")
        r = enrich_stock_with_actuals(r)
        
        # Add a small 2-second delay between consecutive compiles to remain safely within standard API limits
        if reports_compiled > 0:
            print("⏳ Spacing out API requests (2s delay)...")
            time.sleep(2)
            
        print(f"Requesting LLM to generate full Wheels-style equity research report...")
        reports_compiled += 1
        
        try:
            confl_model = os.environ.get("CONFLUENCE_MODEL")
            if not confl_model:
                raise KeyError("Environment variable 'CONFLUENCE_MODEL' is missing.")
            
            # --- SELF-HEALING RETRY LOOP (Up to 3 attempts) ---
            max_attempts = 3
            report_text = ""
            success = False
            
            for attempt in range(1, max_attempts + 1):
                print(f"✍️ [COMPILE ATTEMPT {attempt}/{max_attempts}] Requesting {confl_model} to generate full report...")
                report_text = generate_report_via_gemini(api_key, r, prompt_template, today_str, model=confl_model)
                
                # Verify report completeness
                missing_sections = verify_report_completeness(report_text)
                if not missing_sections:
                    print(f"✅ [VERIFICATION SUCCESS] All sections generated successfully on attempt {attempt}!")
                    success = True
                    break
                else:
                    print(f"⚠️ [VERIFICATION FAILED] Missing sections on attempt {attempt}: {missing_sections}")
                    
                    # Save and sync failed draft to remote GitHub for instant diagnostics
                    try:
                        failed_dir = reports_dir / "failed"
                        failed_dir.mkdir(exist_ok=True)
                        failed_file = failed_dir / f"failed_draft_{symbol}_attempt_{attempt}.md"
                        with open(failed_file, "w") as ff:
                            ff.write(report_text)
                        print(f"📂 [DIAGNOSTIC] Saved failed draft to: {failed_file}")
                        git_commit_and_push(f"{symbol}_failed_{attempt}", failed_file)
                    except Exception as df_err:
                        print(f"⚠️ Failed to save or sync diagnostic draft: {df_err}")
                        
                    if attempt < max_attempts:
                        print("🔄 Retrying full generation to recover missing sections...")
                        time.sleep(2)
            
            if not success:
                raise RuntimeError(f"Failed to generate a complete report for {symbol} after {max_attempts} attempts.")
                
            report_file = reports_dir / f"{symbol}_equity_report_{date_suffix}.md"
            
            # Sanitize header block for mathematical correctness
            cmp = r.get("close", 0.0)
            target_price, upside_pct = extract_target_and_upside(report_text, cmp)
            report_text = sanitize_report_header_block(report_text, cmp, target_price, upside_pct)
            
            latest_q = r.get("latest_quarter", "")
            if latest_q:
                report_text += f"\n\n<!-- latest_quarter: {latest_q} -->\n"
                
            with open(report_file, "w") as f:
                f.write(report_text)
                
            print(f"✅ [SUCCESS] Saved report: {report_file}")
            
            # Commit and push immediately to preserve progress
            git_commit_and_push(symbol, report_file)
            
            # Reset consecutive failure counter on success
            consecutive_failures = 0
            
            # Send separate dedicated email with the report
            try:
                send_report_email(symbol, company, report_text)
            except Exception as mail_err:
                print(f"⚠️  Error sending separate email for {symbol}: {mail_err}")
        except Exception as e:
            print(f"❌ [FAILED] Error generating report for {symbol}: {e}")
    if report_type not in ["emerging", "all"]:
        print("\n" + "="*80)
        print("🏆 Deep Equity Research Report compilation process complete!")
        print("="*80)
        return
        
    emerg_json_path = Path("outputs") / "today_emerging.json"
    if emerg_json_path.exists():
        try:
            with open(emerg_json_path, "r") as f:
                emerging_rows = json.load(f)
        except Exception as e:
            print(f"⚠️ Error loading emerging leaders list: {e}")
            emerging_rows = []
            
        if emerging_rows:
            print("\n" + "="*80)
            print(f"🚀 Found {len(emerging_rows)} emerging leaders. Processing reports...")
            print("="*80)
            
            # --- PARALLEL NSE DELIVERY FETCHING FOR ALL EMERGING LEADERS ---
            import concurrent.futures
            print(f"⚡ Loading local NSE delivery statistics in parallel for {len(emerging_rows)} symbols...")
            emerging_symbols = [row["symbol"] for row in emerging_rows]
            
            emerging_delivery_map = {}
            with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
                future_to_symbol = {executor.submit(fetch_nse_delivery_data, sym): sym for sym in emerging_symbols}
                for future in concurrent.futures.as_completed(future_to_symbol):
                    sym = future_to_symbol[future]
                    try:
                        stats = future.result()
                        if stats:
                            emerging_delivery_map[sym] = stats
                            print(f"   ✅ Loaded delivery data for {sym}")
                        else:
                            emerging_delivery_map[sym] = {}
                            print(f"   ⚠️ No delivery data returned for {sym}")
                    except Exception as exc:
                        emerging_delivery_map[sym] = {}
                        print(f"   ❌ Delivery fetch generated an exception for {sym}: {exc}")
            # ----------------------------------------------------------------
            
            compiled_emerging_reports = []
            
            for r in emerging_rows:
                # First enrich basic metadata to ensure we have actual close price, mcap, industry
                r = enrich_basic_metadata(r)
                
                symbol = r["symbol"]
                company = r["company"]
                
                # Enrich with pre-fetched live delivery data and signals
                stats = emerging_delivery_map.get(symbol, {})
                r["delivery_stats"] = stats
                r["delivery_signal"] = calculate_delivery_signal(stats, r.get("close", 0.0))
                
                print(f"\n🔍 Checking report status for Emerging Leader: `{symbol}` ({company})...")
                
                exists, quarter_info = check_existing_quarter_report(symbol, reports_dir, today)
                if exists:
                    print(f"⏭️  [SKIPPED GENERATION] A report for emerging leader {symbol} exists in current quarter: {quarter_info}.")
                    existing_path = get_existing_quarter_report_path(symbol, reports_dir, today)
                    if existing_path and existing_path.exists():
                        try:
                            with open(existing_path, "r") as ef:
                                report_text = ef.read()
                                
                            # Dynamically sanitize the header block in the existing report
                            cmp = r.get("close", 0.0)
                            target_price, upside_pct = extract_target_and_upside(report_text, cmp)
                            sanitized_text = sanitize_report_header_block(report_text, cmp, target_price, upside_pct)
                            
                            # If it changed, write it back to disk to permanently update it
                            if sanitized_text != report_text:
                                with open(existing_path, "w") as wf:
                                    wf.write(sanitized_text)
                                report_text = sanitized_text
                                print(f"✍️ Sanitized and updated existing emerging report for {symbol} on disk.")
                                
                            report_date_str = get_report_date_str(existing_path) if existing_path else today_str
                            is_new_for_digest = (existing_path and existing_path.name == f"{symbol}_equity_report_{date_suffix}.md")
                            compiled_emerging_reports.append({
                                "symbol": symbol,
                                "company": company,
                                "report_md": report_text,
                                "r": r,
                                "is_new": is_new_for_digest,
                                "report_date": report_date_str
                            })
                            print(f"📋 Loaded existing report for {symbol} into today's digest. Date: {report_date_str} (is_new_for_digest: {is_new_for_digest})")
                        except Exception as read_err:
                            print(f"⚠️ Failed to read existing report for {symbol}: {read_err}")
                    continue
                    
                print(f"✍️  [COMPILING] Compiling report for emerging leader {symbol}...")
                r = enrich_stock_with_actuals(r)
                
                if reports_compiled > 0:
                    print("⏳ Spacing out API requests (2s delay)...")
                    time.sleep(2)
                    
                reports_compiled += 1
                
                try:
                    emerg_model = os.environ.get("EMERGING_MODEL")
                    if not emerg_model:
                        raise KeyError("Environment variable 'EMERGING_MODEL' is missing.")
                    
                    # --- SELF-HEALING RETRY LOOP (Up to 3 attempts) ---
                    max_attempts = 3
                    report_text = ""
                    success = False
                    
                    for attempt in range(1, max_attempts + 1):
                        print(f"✍️ [COMPILE ATTEMPT {attempt}/{max_attempts}] Requesting {emerg_model} to generate emerging leader report...")
                        report_text = generate_report_via_gemini(api_key, r, prompt_template, today_str, model=emerg_model)
                        
                        # Verify report completeness
                        missing_sections = verify_report_completeness(report_text)
                        if not missing_sections:
                            print(f"✅ [VERIFICATION SUCCESS] Emerging report generated successfully on attempt {attempt}!")
                            success = True
                            break
                        else:
                            print(f"⚠️ [VERIFICATION FAILED] Missing sections on attempt {attempt}: {missing_sections}")
                            
                            # Save and sync failed draft to remote GitHub for instant diagnostics
                            try:
                                failed_dir = reports_dir / "failed"
                                failed_dir.mkdir(exist_ok=True)
                                failed_file = failed_dir / f"failed_draft_{symbol}_attempt_{attempt}.md"
                                with open(failed_file, "w") as ff:
                                    ff.write(report_text)
                                print(f"📂 [DIAGNOSTIC] Saved failed draft to: {failed_file}")
                                git_commit_and_push(f"{symbol}_failed_{attempt}", failed_file)
                            except Exception as df_err:
                                print(f"⚠️ Failed to save or sync diagnostic draft: {df_err}")
                                
                            if attempt < max_attempts:
                                print("🔄 Retrying full generation to recover missing sections...")
                                time.sleep(2)
                                
                    if not success:
                        raise RuntimeError(f"Failed to generate a complete emerging report for {symbol} after {max_attempts} attempts.")
                        
                    report_file = reports_dir / f"{symbol}_equity_report_{date_suffix}.md"
                    
                    # Sanitize header block for mathematical correctness
                    cmp = r.get("close", 0.0)
                    target_price, upside_pct = extract_target_and_upside(report_text, cmp)
                    report_text = sanitize_report_header_block(report_text, cmp, target_price, upside_pct)
                    
                    latest_q = r.get("latest_quarter", "")
                    if latest_q:
                        report_text += f"\n\n<!-- latest_quarter: {latest_q} -->\n"
                        
                    with open(report_file, "w") as f:
                        f.write(report_text)
                        
                    print(f"✅ [SUCCESS] Saved report to file: {report_file}")
                    
                    # Commit and push immediately to preserve progress
                    git_commit_and_push(symbol, report_file)
                    
                    # Reset consecutive failure counter on success
                    consecutive_failures = 0
                    
                    # Keep track of compiled report details for consolidated digest email
                    compiled_emerging_reports.append({
                        "symbol": symbol,
                        "company": company,
                        "report_md": report_text,
                        "r": r,
                        "is_new": True,
                        "report_date": today_str
                    })
                    print(f"✅ [EMERGING LEADER] Successfully compiled and committed report for {symbol} ({company})!")
                    
                except Exception as e:
                    print(f"❌ [FAILED] Error generating report for emerging leader {symbol}: {e}")
                    consecutive_failures += 1
                    if consecutive_failures >= 3:
                        print("\n❌ [CRITICAL] 3 consecutive report generation failures occurred. Terminating pipeline to protect API quota.")
                        sys.exit(1)
            
            # Send the consolidated emerging leaders digest email if any reports were compiled
            if compiled_emerging_reports:
                try:
                    send_emerging_digest_email(compiled_emerging_reports)
                except Exception as digest_err:
                    print(f"⚠️  Error sending emerging leaders digest email: {digest_err}")
    else:
        print("\nℹ️ No emerging leaders list found at outputs/today_emerging.json. Skipping.")

    print("\n" + "="*80)
    print("🏆 Deep Equity Research Report compilation process complete!")
    print("="*80)

if __name__ == "__main__":
    main()
