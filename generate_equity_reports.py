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
from datetime import datetime, date
from pathlib import Path

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

# --- Live StockScans and yfinance actuals scrapers ---

def fetch_stockscans_company_data(symbol: str) -> dict:
    """
    Fetch all available fundamental, peer, and card details from StockScans for a symbol.
    """
    cookie = os.environ.get(
        "STOCKSCANS_COOKIE", 
        "ext_name=ojplmecpdpgccookcobabopnaifgidhf; theme=light; _clck=lwn8kd%5E2%5Eg5g%5E0%5E2304; authtoken=eyJ0eXAiOiJKV1QiLCJhbGciOiJIUzI1NiJ9.eyJleHAiOjE3ODA4MDU0NTAsInVzZXJJZCI6IjY2MjM3MGFkN2IyYzAyMDEwZjQ0NTU5NyJ9.fG9VwT-Gu8i8H0JBpT6WzJMgKiPeFF73x6QDS0DT7vA"
    )
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
            
    return {
        "symbol": symbol,
        "exchange": exchange,
        "source": source,
        "search": search_data,
        "fundamentals": fundamentals_data,
        "peers": peers_list,
        "card_details": card_details,
        "shareholding": shareholding_data
    }


def fetch_peers_fundamentals_in_parallel(peer_ids: list[str]) -> dict:
    """
    Fetch fundamentals for multiple peer symbols in parallel.
    """
    results = {}
    if not peer_ids:
        return results
        
    from concurrent.futures import ThreadPoolExecutor, as_completed
    cookie = os.environ.get(
        "STOCKSCANS_COOKIE", 
        "ext_name=ojplmecpdpgccookcobabopnaifgidhf; theme=light; _clck=lwn8kd%5E2%5Eg5g%5E0%5E2304; authtoken=eyJ0eXAiOiJKV1QiLCJhbGciOiJIUzI1NiJ9.eyJleHAiOjE3ODA4MDU0NTAsInVzZXJJZCI6IjY2MjM3MGFkN2IyYzAyMDEwZjQ0NTU5NyJ9.fG9VwT-Gu8i8H0JBpT6WzJMgKiPeFF73x6QDS0DT7vA"
    )
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
        "shareholding_table": ""
    }
    
    # 1. Format Yearly Financial Statements
    if yearly_data and len(yearly_data) > 1:
        headers = yearly_data[0]
        rows = yearly_data[1:]
        header_map = {h: i for i, h in enumerate(headers)}
        row_map = {r[header_map["Date"]]: r for r in rows if "Date" in header_map}
        
        available_years = sorted(list(row_map.keys()))
        years_to_show = available_years[-5:] if len(available_years) >= 5 else available_years
        
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
        
        inc_hdr = "| Particulars | " + " | ".join(years_to_show) + " |"
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
        
        bs_hdr = "| Particulars | " + " | ".join(years_to_show) + " |"
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
        
        ratio_hdr = "| Particulars | " + " | ".join(years_to_show) + " |"
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
        
    return formatted


def fetch_nse_delivery_data(symbol: str) -> dict:
    """
    Fetch historical delivery quantity and calculate weekly medians.
    First checks outputs/today_delivery_data.json cache to bypass cloud IP blocks.
    """
    cache_path = Path("outputs") / "today_delivery_data.json"
    if cache_path.exists():
        try:
            with open(cache_path, "r") as f:
                cache = json.load(f)
            if symbol in cache and cache[symbol]:
                if "latest_delivery_pct" in cache[symbol]:
                    print(f"📦 [CACHE HIT] Loaded delivery data from cache for {symbol}")
                    return cache[symbol]
        except Exception as e:
            print(f"⚠️ Error reading delivery cache for {symbol}: {e}")

    # Fallback to live fetch
    from datetime import datetime, timedelta
    try:
        from nselib import capital_market
        import pandas as pd
    except ImportError:
        print("⚠️ nselib or pandas not available for delivery data fetch.")
        return {}

    end_date = datetime.today()
    start_date = end_date - timedelta(days=20)
    
    from_date_str = start_date.strftime("%d-%m-%Y")
    to_date_str = end_date.strftime("%d-%m-%Y")
    
    try:
        df = capital_market.price_volume_and_deliverable_position_data(
            symbol=symbol,
            from_date=from_date_str,
            to_date=to_date_str
        )
        if df.empty:
            return {}
            
        df.columns = [c.replace('ï»¿', '').replace('"', '') for c in df.columns]
        
        df['ParsedDate'] = pd.to_datetime(df['Date'], format='%d-%b-%Y', errors='coerce')
        if df['ParsedDate'].isna().all():
            df['ParsedDate'] = pd.to_datetime(df['Date'], format='%d-%m-%Y', errors='coerce')
            
        df = df.dropna(subset=['ParsedDate']).sort_values('ParsedDate')
        
        # Clean commas and convert all numeric columns to float/numeric
        for col in ['DeliverableQty', '%DlyQttoTradedQty', 'TotalTradedQuantity', 'ClosePrice', 'TurnoverInRs']:
            if col in df.columns:
                if df[col].dtype == object:
                    df[col] = df[col].astype(str).str.replace(',', '', regex=False)
                df[col] = pd.to_numeric(df[col], errors='coerce')
        
        df = df.dropna(subset=['DeliverableQty', '%DlyQttoTradedQty', 'TotalTradedQuantity', 'ClosePrice', 'TurnoverInRs'])
        
        if df.empty:
            return {}
            
        df_week = df.tail(5)
        latest_row = df.iloc[-1]
        
        # Calculate traded and deliverable value in Rs. Cr
        df['DeliveryValueCr'] = (df['DeliverableQty'] * df['ClosePrice']) / 10000000.0
        df['TradedValueCr'] = df['TurnoverInRs'] / 10000000.0
        
        df_week = df.tail(5)
        latest_row = df.iloc[-1]
        
        return {
            "latest_date": latest_row['Date'],
            "latest_traded_qty": float(latest_row['TotalTradedQuantity']),
            "latest_delivery_qty": float(latest_row['DeliverableQty']),
            "latest_delivery_pct": float(latest_row['%DlyQttoTradedQty']),
            "latest_traded_val_cr": float(latest_row['TradedValueCr']),
            "latest_delivery_val_cr": float(latest_row['DeliveryValueCr']),
            "week_delivery_qty_median": float(df_week['DeliverableQty'].median()),
            "week_delivery_pct_median": float(df_week['%DlyQttoTradedQty'].median()),
            "week_traded_qty_median": float(df_week['TotalTradedQuantity'].median()),
            "week_traded_val_median_cr": float(df_week['TradedValueCr'].median()),
            "week_delivery_val_median_cr": float(df_week['DeliveryValueCr'].median())
        }
    except Exception as e:
        print(f"⚠️ Error fetching delivery data from nselib for {symbol}: {e}")
        return {}


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
        r["ss_peer_table"] = tables.get("peer_table") or ""
        r["ss_income_statement"] = tables.get("income_statement") or ""
        r["ss_balance_sheet"] = tables.get("balance_sheet") or ""
        r["ss_cash_flow_ratios"] = tables.get("cash_flow_ratios") or ""
        r["ss_shareholding_table"] = tables.get("shareholding_table") or ""
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
        "HEADER BLOCK (Rating & Target)": [r"Rating", r"12M Target Price"],
        "SECTION 2 (Investment Thesis)": [r"INVESTMENT THESIS"],
        "SECTION 3 (Business Overview)": [r"BUSINESS OVERVIEW"],
        "SECTION 4 (Industry Landscape)": [r"INDUSTRY", r"COMPETITIVE"],
        "SECTION 5 (Management Quality)": [r"MANAGEMENT", r"CAPITAL ALLOCATION"],
        "SECTION 6 (Financial Statements)": [r"FINANCIAL DEEP-DIVE", r"INCOME STATEMENT", r"BALANCE SHEET"],
        "SECTION 7 (Earnings Quality)": [r"EARNINGS QUALITY"],
        "SECTION 8 (Valuation Scenarios)": [r"VALUATION", r"BULL", r"BASE", r"BEAR"],
        "SECTION 9 (Key Risks)": [r"RISK"],
        "SECTION 10 (Recommendation)": [r"RECOMMENDATION"],
        "SECTION 10B (Technical Chart Levels)": [r"TECHNICAL LEVELS", r"EMA"],
        "APPENDIX (Latest Concall Brief)": [r"CONCALL BRIEF", r"CONCALL"],
        "DISCLAIMER (Global style rules)": [r"DISCLAIMER"]
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

def check_existing_quarter_report(symbol: str, reports_dir: Path, today: datetime) -> tuple[bool, str]:
    """Check if a report for this symbol already exists in the same calendar quarter as today."""
    if os.environ.get("FORCE_COMPILE", "false").lower() == "true":
        return False, ""
        
    if not reports_dir.exists():
        return False, ""
        
    current_q, current_y = get_calendar_quarter(today)
    prefix = f"{symbol}_equity_report_"
    
    for filepath in reports_dir.glob(f"{prefix}*.md"):
        filename = filepath.name
        # Format can be symbol_equity_report_YYYY-MM-DD.md
        # Extract the date part
        try:
            date_str = filename.replace(prefix, "").replace(".md", "")
            # Support YYYY-MM-DD format
            file_date = datetime.strptime(date_str, "%Y-%m-%d")
            file_q, file_y = get_calendar_quarter(file_date)
            
            if file_q == current_q and file_y == current_y:
                return True, f"Q{file_q} {file_y} (generated on {date_str})"
        except Exception:
            # Fallback to file modification time if filename parsing fails
            try:
                mtime = datetime.fromtimestamp(filepath.stat().st_mtime)
                file_q, file_y = get_calendar_quarter(mtime)
                if file_q == current_q and file_y == current_y:
                    return True, f"Q{file_q} {file_y} (file modification time: {mtime.strftime('%Y-%m-%d')})"
            except Exception:
                pass
                
    return False, ""

def get_existing_quarter_report_path(symbol, reports_dir, today):
    """Return the Path of the existing report for this symbol in the same calendar quarter, if any."""
    if not reports_dir.exists():
        return None
        
    current_q, current_y = get_calendar_quarter(today)
    prefix = f"{symbol}_equity_report_"
    
    for filepath in reports_dir.glob(f"{prefix}*.md"):
        filename = filepath.name
        try:
            date_str = filename.replace(prefix, "").replace(".md", "")
            file_date = datetime.strptime(date_str, "%Y-%m-%d")
            file_q, file_y = get_calendar_quarter(file_date)
            if file_q == current_q and file_y == current_y:
                return filepath
        except Exception:
            try:
                mtime = datetime.fromtimestamp(filepath.stat().st_mtime)
                file_q, file_y = get_calendar_quarter(mtime)
                if file_q == current_q and file_y == current_y:
                    return filepath
            except Exception:
                pass
    return None

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

def generate_report_via_gemini(api_key: str, r: dict, prompt_template: str, today_str: str, model: str = None) -> str:
    """Invoke the Gemini API in three distinct stages to guarantee complete, non-truncated reports."""
    symbol = r["symbol"]
    company = r["company"]
    sector = r["industry"]
    cmp = r.get("close", 0.0)
    mcap = r.get("mcap_cr", 0.0)
    
    # Extract actual ratios and data if available
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
    promoter = yf_info.get("heldPercentInsiders", 0.0) * 100.0
    inst_held = yf_info.get("heldPercentInstitutions", 0.0) * 100.0
    fii = inst_held * 0.6 # estimate split if not exact
    dii = inst_held * 0.4
    public_val = 100.0 - promoter - fii - dii
    
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
                
    # Build metadata block with formatted actuals
    metadata = f"""
COMPANY: {company}
NSE TICKER: {symbol}
SECTOR: {sector}
REPORT DATE: {today_str}
CMP: Rs. {cmp:.2f}
MARKET CAP: Rs. {mcap:.1f} Cr
YOUR RATING: BUY
12M TARGET: (Please calculate dynamically based on peer multiples, financial data, and your valuation modeling)

--- ACTUAL FINANCIAL RATIOS AND DATA FOR HEADER BLOCK ---
P/E (TTM): {pe:.2f}x
P/B (TTM): {pb:.2f}x
ROCE: {roce:.2f}%
ROE: {roe:.2f}%
EPS (latest full year): {ss_meta.get("EPS", 0.0):.2f}
Book Value: Rs. {bv:.2f}
Dividend Yield: {dy:.2f}%
Face Value: Rs. {yf_info.get("faceValue") or 10}
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

    
    # Dual-model routing support
    if not model:
        model = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")
        
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
                if response.status_code in [429, 503]:
                    print(f"⚠️ [Attempt {attempt}/{max_retries}] API returned {response.status_code}. Retrying in {delay}s...")
                    time.sleep(delay)
                    delay *= 2
                    continue
                response.raise_for_status()
                return response.json()
            except Exception as e:
                if attempt == max_retries:
                    raise e
                print(f"⚠️ [Attempt {attempt}/{max_retries}] Exception: {e}. Retrying in {delay}s...")
                time.sleep(delay)
                delay *= 2
        return None

    def call_stage_with_fallback(stage_num: int, prompt_text: str, expected_headers: list[str], primary_model: str) -> str:
        # We try primary_model first. If it's gemini-2.5-flash, we set thinkingBudget: 0.
        models_to_try = [primary_model]
        # If the primary model is 2.5, add gemini-flash-latest as the fallback
        if "2.5" in primary_model and "gemini-flash-latest" not in models_to_try:
            models_to_try.append("gemini-flash-latest")
            
        for attempt_model in models_to_try:
            print(f"🤖 [STAGE {stage_num}] Requesting model {attempt_model}...")
            
            # Setup payload with thinkingConfig if model is 2.5, latest, or pro
            gen_config = {"temperature": 0.7, "maxOutputTokens": 8192}
            if "2.5" in attempt_model or "latest" in attempt_model or "pro" in attempt_model:
                gen_config["thinkingConfig"] = {"thinkingBudget": 0}
                
            payload = {
                "contents": [{"parts": [{"text": prompt_text}]}],
                "generationConfig": gen_config
            }
            
            stage_url = f"https://generativelanguage.googleapis.com/v1beta/models/{attempt_model}:generateContent?key={api_key}"
            
            res_json = call_gemini_with_retry(stage_url, payload)
            if not res_json or "candidates" not in res_json:
                print(f"⚠️ [STAGE {stage_num}] Failed API call for {attempt_model}. Trying next option...")
                continue
                
            candidate = res_json["candidates"][0]
            finish_reason = candidate.get("finishReason")
            
            if "content" not in candidate or "parts" not in candidate["content"]:
                print(f"⚠️ [STAGE {stage_num}] No content in candidate from {attempt_model}. Trying next option...")
                continue
                
            text = candidate["content"]["parts"][0]["text"].strip()
            
            # Validate completion
            missing_headers = []
            for h in expected_headers:
                if not re.search(h, text, re.IGNORECASE):
                    missing_headers.append(h)
                    
            if finish_reason == "MAX_TOKENS" or missing_headers:
                print(f"⚠️ [STAGE {stage_num}] Incomplete output using {attempt_model} (finishReason: {finish_reason}, missing headers: {missing_headers})")
                if len(models_to_try) > 1 and attempt_model == primary_model:
                    print(f"🔄 [STAGE {stage_num}] Falling back from {primary_model} to {models_to_try[1]}...")
                    continue
            
            # Successful run
            print(f"✅ [STAGE {stage_num}] Successfully generated via {attempt_model}!")
            return text
            
        raise RuntimeError(f"Stage {stage_num} failed completely on all available models.")

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
        f"4. Format the Header Block metrics as exactly two wide horizontal tables stacked vertically.\n"
        f"5. {whitespace_rule}\n\n"
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
        actuals_context += (
            f"--- ACTUAL FINANCIAL STATEMENT TABLES FROM STOCKSCANS ---\n"
            f"You MUST use these exact tables for TABLE 1, TABLE 2, and TABLE 3 in SECTION 6. Do not modify the numbers for past years.\n"
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
        f"4. Under no circumstances should you generate SECTION 8 or beyond in this call. Stop generating immediately after Section 7.\n"
        f"5. Maintain absolute mathematical and analytical consistency with the rating, prices, and metrics established in PART 1.\n"
        f"6. {whitespace_rule}\n\n"
        f"Here is the context of PART 1 generated previously for consistency:\n"
        f"--- START OF PART 1 CONTEXT ---\n"
        f"{compact_context}\n"
        f"--- END OF PART 1 CONTEXT ---\n\n"
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
        f"5. CRITICAL DENSITY RULE: Keep all Stage 3 sections extremely dense and concise to prevent text truncation:\n"
        f"   - SECTION 9 (Key Risks): List exactly 5-6 core risks with a 1-line description and 1-line monitoring metric each.\n"
        f"   - SECTION 10B (Technical EMAs & Chart Levels): Provide highly precise, compact, single-line answers for all indicators.\n"
        f"   - APPENDIX (Latest Concall Brief): Summarize each of the 10 subsections in exactly 1-2 punchy, data-filled bullet points. Keep it extremely dense and free of empty transition phrases.\n"
        f"6. {whitespace_rule}\n\n"
        f"Here is the context of PART 1 and PART 2 generated previously for consistency:\n"
        f"--- START OF CONTEXT ---\n"
        f"{compact_context_part3}\n"
        f"{actuals_del_context}"
        f"--- END OF CONTEXT ---\n\n"
        f"Now, generate PART 3 (starting from ### SECTION 8 — VALUATION) for {company} ({symbol}):"
    )

    stage3_headers = ["SECTION 8", "SECTION 9", "SECTION 10", "TECHNICAL LEVELS|SECTION 10B", "CONCALL BRIEF|APPENDIX", "DISCLAIMER"]
    part3_text = call_stage_with_fallback(3, stage3_prompt, stage3_headers, model)
    
    # Combine all three parts beautifully
    full_report = part1_text + "\n\n" + part2_text + "\n\n" + part3_text
    return full_report


REPORT_STYLESHEET = """
          .report-h4 {
            color: #2d3748;
            font-size: 15px;
            margin: 20px 0 10px 0;
            border-left: 3px solid #1b365d;
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
    md_text = re.sub(r"\*\*(.*?)\*\*", r"<strong>\1</strong>", md_text)
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
            html_lines.append(f'<h4 style="color: #2d3748; font-size: 15px; margin: 20px 0 10px 0; border-left: 3px solid #1b365d; padding-left: 10px; font-weight: bold; font-family: sans-serif;">{h_text}</h4>')
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
            if ":---" in line_strip or "---:" in line_strip or "-|-" in line_strip:
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
          <td style="border: 1px solid #e2e8f0; padding: 10px; color: #2d3748; font-family: sans-serif; white-space: nowrap;">{date_display}</td>
          <td style="border: 1px solid #e2e8f0; padding: 10px; text-align: right; color: #2d3748; font-family: sans-serif;">₹{cmp:,.2f}</td>
          {del_summary_td}
          <td style="border: 1px solid #e2e8f0; padding: 10px; text-align: right; font-weight: bold; color: #2e7d32; font-family: sans-serif;">₹{target_price:,.2f} (+{upside_pct:.1f}%)</td>
          <td style="border: 1px solid #e2e8f0; padding: 10px; text-align: right; color: #4a5568; font-family: sans-serif;">₹{mcap:,.1f} Cr</td>
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
          <td style="border: 1px solid #e2e8f0; padding: 10px; color: #2d3748; font-family: sans-serif; white-space: nowrap;">{date_display}</td>
          <td style="border: 1px solid #e2e8f0; padding: 10px; text-align: right; color: #2d3748; font-family: sans-serif;">₹{cmp:,.2f}</td>
          {del_summary_td}
          <td style="border: 1px solid #e2e8f0; padding: 10px; text-align: right; font-weight: bold; color: #2e7d32; font-family: sans-serif;">₹{target_price:,.2f} (+{upside_pct:.1f}%)</td>
          <td style="border: 1px solid #e2e8f0; padding: 10px; text-align: right; color: #4a5568; font-family: sans-serif;">₹{mcap:,.1f} Cr</td>
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
            <th style="border: 1px solid #e2e8f0; padding: 12px 10px; text-align: left; font-family: sans-serif; font-size: 14px;">Report Date</th>
            <th style="border: 1px solid #e2e8f0; padding: 12px 10px; text-align: right; font-family: sans-serif; font-size: 14px;">CMP</th>
            <th style="border: 1px solid #e2e8f0; padding: 12px 10px; text-align: center; font-family: sans-serif; font-size: 14px;">Volume & Delivery (Live)</th>
            <th style="border: 1px solid #e2e8f0; padding: 12px 10px; text-align: right; font-family: sans-serif; font-size: 14px;">12M Target (Upside)</th>
            <th style="border: 1px solid #e2e8f0; padding: 12px 10px; text-align: right; font-family: sans-serif; font-size: 14px;">MCap (Cr)</th>
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
    try:
        print(f"📦 [GIT] Syncing {symbol} report to remote repository...")
        # Configure user details locally to prevent commit blocks on fresh VMs
        subprocess.run(["git", "config", "user.name", "github-actions[bot]"], check=False)
        subprocess.run(["git", "config", "user.email", "github-actions[bot]@users.noreply.github.com"], check=False)
        
        # Add the report file
        subprocess.run(["git", "add", "-f", str(report_file)], check=True)
        
        # Check if there is anything to commit
        diff_res = subprocess.run(["git", "diff", "--quiet", "--staged"], check=False)
        if diff_res.returncode != 0:
            # Commit the staged file
            subprocess.run(["git", "commit", "-m", f"chore: auto-publish equity report for {symbol} [skip ci]"], check=True)
            # Rebase autostash pull to ensure we integrate any concurrent remote updates safely
            subprocess.run(["git", "pull", "--rebase", "--autostash", "origin", "main"], check=True)
            # Push to the remote branch
            subprocess.run(["git", "push", "origin", "main"], check=True)
            print(f"🚀 [GIT] Successfully synced {symbol} report to GitHub!")
        else:
            print(f"ℹ️ [GIT] No changes detected for {symbol} report (already synced).")
    except Exception as e:
        print(f"⚠️ [GIT WARNING] Failed to auto-sync {symbol} report: {e}")

def main() -> None:
    print("="*80)
    print("🌟🚀 AUTOMATED MONIT DEEP EQUITY RESEARCH PIPELINE 🚀🌟")
    print("="*80)
    
    # 1. Verify Gemini API credentials
    api_key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
    if not api_key:
        print("❌ Error: GEMINI_API_KEY environment variable is not set!")
        print("Please configure your Gemini API Key in the environment or .env file.")
        sys.exit(1)
        
    # 2. Check for confluence list JSON
    confl_json_path = Path("outputs") / "today_confluences.json"
    if not confl_json_path.exists():
        print(f"ℹ️ Confluences list not found at: {confl_json_path}")
        print("Please run the main daily pipeline first (monit_ranker.py) to output confluences.")
        sys.exit(0)
        
    try:
        with open(confl_json_path, "r") as f:
            confluence_3_rows = json.load(f)
    except Exception as e:
        print(f"❌ Error loading confluences list: {e}")
        sys.exit(1)
        
    if not confluence_3_rows:
        print("ℹ️ Today's confluence list is empty. No triple-confluence stocks detected today.")
        sys.exit(0)
        
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
    
    print(f"Found {len(confluence_3_rows)} triple-confluence candidates. Processing all candidates...")
    
    # Process all confluences to keep speed high and costs optimal
    reports_compiled = 0
    consecutive_failures = 0
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
                except Exception as sanitize_err:
                    print(f"⚠️ Failed to sanitize existing confluence report for {symbol}: {sanitize_err}")
            continue
            
        print(f"✍️  [COMPILING] No report found for {symbol} in the current calendar quarter.")
        r = enrich_stock_with_actuals(r)
        
        # Add a small 2-second delay between consecutive compiles to remain safely within standard API limits
        if reports_compiled > 0:
            print("⏳ Spacing out API requests (2s delay)...")
            time.sleep(2)
            
        print(f"Requesting Gemini AI to generate full Wheels-style equity research report...")
        reports_compiled += 1
        
        try:
            confl_model = os.environ.get("CONFLUENCE_MODEL", "gemini-2.5-flash")
            
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
                # Load and process emerging leaders (save file, commit, and send consolidated digest email)
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
            print(f"⚡ Fetching live NSE delivery statistics in parallel for {len(emerging_rows)} symbols...")
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
                            print(f"   ✅ Fetched live delivery data for {sym}")
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
                            compiled_emerging_reports.append({
                                "symbol": symbol,
                                "company": company,
                                "report_md": report_text,
                                "r": r,
                                "is_new": False,
                                "report_date": report_date_str
                            })
                            print(f"📋 Loaded existing report for {symbol} into today's digest. Date: {report_date_str}")
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
                    emerg_model = os.environ.get("EMERGING_MODEL", "gemini-2.5-flash")
                    
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
