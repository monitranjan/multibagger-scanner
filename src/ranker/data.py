from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pandas as pd
import requests
import yfinance as yf
from bs4 import BeautifulSoup

from ranker.indicators import map_stockscans_index_to_yfinance

SCAN_CLAUSE = (
    "( {cash} ( "
    " daily close > daily ema ( daily close , 200 ) "
    "and daily close > ( daily max ( 252 , daily high ) * 0.80 ) "
    "and daily rsi(14) > 55 "
    "and market cap >= 500 "
    ") )"
)

METADATA_COLUMNS = (
    "sector as 'Sector', "
    "industry as 'Industry', "
    "marketcapname as 'Marketcap Name', "
    "market cap as 'Market Cap'"
)

STOCKSCANS_STATUS = {"status": "success", "message": "", "fetched_live": False}

try:
    yf.set_tz_cache_location("logs/yfinance_cache")
except Exception:
    pass

def fetch_all_stockscans_indices(symbols: list[str]) -> dict[str, tuple[str, str]]:
    """Fetch indices list for all symbols from StockScans API in parallel."""
    cookie = os.environ.get("STOCKSCANS_COOKIE", "")
    headers = {
        "accept": "application/json",
        "cookie": cookie,
        "user-agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, Gecko) Chrome/148.0.0.0 Safari/537.36"
    }
    
    print(f"Fetching StockScans indices data for {len(symbols)} stocks in parallel...")
    results = {}
    
    def fetch_one(symbol: str):
        for exchange in ["NSE", "BSE"]:
            url = f"https://www.stockscans.in/api/company/indices/{exchange}:{symbol}"
            try:
                r = requests.get(url, headers=headers, timeout=10)
                if r.status_code == 200:
                    data = r.json()
                    indices = data.get("indices", [])
                    if indices:
                        for idx_item in indices:
                            mapped = map_stockscans_index_to_yfinance(idx_item.get("companyId"), idx_item.get("Name"))
                            if mapped:
                                return symbol, mapped
                        first_idx = indices[0]
                        return symbol, (first_idx.get("companyId"), first_idx.get("Name"))
            except Exception:
                continue
        return symbol, None

    from concurrent.futures import ThreadPoolExecutor, as_completed
    with ThreadPoolExecutor(max_workers=30) as executor:
        futures = {executor.submit(fetch_one, sym): sym for sym in symbols}
        for future in as_completed(futures):
            sym = futures[future]
            try:
                symbol, mapped = future.result()
                if mapped:
                    results[symbol] = mapped
            except Exception as e:
                print(f"⚠️ Error fetching StockScans indices for {sym}: {e}")
                
    return results

def fetch_benchmark_indices() -> dict[str, dict]:
    """Fetch 2-year weekly history for Nifty benchmark indices."""
    benchmarks = [
        "^NSEI", "^CRSLDX", "^NSEBANK", "^CNXIT", "^CNXPHARMA", 
        "^CNXFMCG", "^CNXAUTO", "^CNXMETAL", "^CNXREALTY", 
        "^CNXENERGY", "^CNXINFRA", "^CNXMEDIA", "^CNXPSUBANK"
    ]
    print(f"Fetching weekly data for {len(benchmarks)} benchmark indices in parallel...")
    results = {}
    
    def fetch_one(ticker_symbol: str):
        for attempt in range(3):
            try:
                ticker = yf.Ticker(ticker_symbol)
                hist = ticker.history(period="2y", interval="1wk")
                if hist is not None and not hist.empty:
                    return ticker_symbol, {
                        "close": hist["Close"].squeeze(),
                        "high": hist["High"].squeeze(),
                        "low": hist["Low"].squeeze(),
                        "volume": hist["Volume"].squeeze(),
                    }
            except Exception as e:
                time.sleep(0.5)
        print(f"⚠️ Error fetching benchmark index {ticker_symbol}")
        return ticker_symbol, {}

    from concurrent.futures import ThreadPoolExecutor, as_completed
    with ThreadPoolExecutor(max_workers=10) as executor:
        futures = {executor.submit(fetch_one, t): t for t in benchmarks}
        for future in as_completed(futures):
            t, data = future.result()
            if data:
                results[t] = data
                
    return results

def fetch_single_stock_details(symbol: str) -> tuple[str, dict]:
    """Download history and info for a single stock."""
    # Gentle sleep to prevent hitting API limits
    time.sleep(0.02)
    for suffix in [".NS", ".BO"]:
        ticker_symbol = symbol + suffix
        try:
            ticker = yf.Ticker(ticker_symbol)
            hist = ticker.history(period="2y", interval="1wk")
            if hist is not None and len(hist) >= 30:
                info = ticker.info
                return symbol, {
                    "info": info,
                    "close": hist["Close"].squeeze(),
                    "high": hist["High"].squeeze(),
                    "low": hist["Low"].squeeze(),
                    "volume": hist["Volume"].squeeze(),
                    "history_len": len(hist),
                }
        except Exception:
            continue
    return symbol, {}

def fetch_all_stocks_details(symbols: list[str]) -> dict[str, dict]:
    """Fetch yfinance details for a list of stock symbols in parallel."""
    print(f"Fetching details for {len(symbols)} stocks in parallel (30 threads)...")
    results = {}
    from concurrent.futures import ThreadPoolExecutor, as_completed
    with ThreadPoolExecutor(max_workers=30) as executor:
        futures = {executor.submit(fetch_single_stock_details, sym): sym for sym in symbols}
        for i, future in enumerate(as_completed(futures), 1):
            sym = futures[future]
            try:
                symbol, data = future.result()
                if data:
                    results[symbol] = data
                    if i % 30 == 0 or i == len(symbols):
                        print(f"  [{i}/{len(symbols)}] Fetched details for {symbol}")
            except Exception as e:
                print(f"  [{i}/{len(symbols)}] Error fetching {sym}: {e}")
    return results

def fetch_chartink_universe() -> pd.DataFrame:
    """Fetch base Chartink rows and enrich them with sector/industry/market cap."""
    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36"
            )
        }
    )
    r = session.get("https://chartink.com/screener", timeout=90)
    r.raise_for_status()
    csrf = BeautifulSoup(r.content, "html.parser").find(
        "meta", {"name": "csrf-token"}
    )["content"]
    headers = {
        "Referer": "https://chartink.com/screener",
        "x-csrf-token": csrf,
        "X-Requested-With": "XMLHttpRequest",
    }

    base = _post_chartink(session, headers, {"scan_clause": SCAN_CLAUSE})
    meta = _post_chartink(
        session,
        headers,
        {"scan_clause": SCAN_CLAUSE, "column_clause": METADATA_COLUMNS},
    )
    meta_cols = [
        "nsecode",
        "sector",
        "industry",
        "marketcap name",
        "market cap",
    ]
    enriched = base.merge(meta[meta_cols], on="nsecode", how="left")
    enriched = enriched.rename(
        columns={
            "nsecode": "symbol",
            "name": "company",
            "bsecode": "bse_code",
            "per_chg": "today_return_pct",
            "marketcap name": "marketcap_bucket",
            "market cap": "marketcap_cr",
        }
    )
    numeric_cols = ["close", "today_return_pct", "volume", "marketcap_cr"]
    for col in numeric_cols:
        enriched[col] = pd.to_numeric(enriched[col], errors="coerce")
    return enriched.sort_values("today_return_pct", ascending=False).reset_index(
        drop=True
    )

def _post_chartink(
    session: requests.Session, headers: dict[str, str], payload: dict[str, str]
) -> pd.DataFrame:
    resp = session.post(
        "https://chartink.com/screener/process",
        headers=headers,
        data=payload,
        timeout=120,
    )
    resp.raise_for_status()
    data = resp.json()
    if data.get("scan_error"):
        raise RuntimeError(f"Chartink scan error: {data['scan_error']}")
    return pd.DataFrame(data.get("data", []))

def fetch_single_stockscans_details(symbol: str) -> tuple[str, dict]:
    """Fetch StockScans search-company data dynamically using your authtoken."""
    cookie = os.environ.get("STOCKSCANS_COOKIE", "")
    headers = {
        "accept": "application/json",
        "cookie": cookie,
        "user-agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, Gecko) Chrome/148.0.0.0 Safari/537.36"
    }
    # Gentle wait to avoid rate limit
    time.sleep(0.01)
    for exchange in ["NSE", "BSE"]:
        url = f"https://www.stockscans.in/api/company/scans/search-company/{exchange}:{symbol}"
        try:
            r = requests.get(url, headers=headers, timeout=12)
            if r.status_code == 200:
                return symbol, r.json()
        except Exception:
            continue
    return symbol, {}

def fetch_all_stockscans_details(symbols: list[str]) -> dict[str, dict]:
    """Fetch StockScans search-company data in parallel using 30 threads."""
    print(f"Fetching StockScans search-company data for {len(symbols)} stocks in parallel...")
    results = {}
    from concurrent.futures import ThreadPoolExecutor, as_completed
    with ThreadPoolExecutor(max_workers=30) as executor:
        futures = {executor.submit(fetch_single_stockscans_details, sym): sym for sym in symbols}
        for i, future in enumerate(as_completed(futures), 1):
            sym = futures[future]
            try:
                symbol, data = future.result()
                if data:
                    results[symbol] = data
                    if i % 50 == 0 or i == len(symbols):
                        print(f"  [{i}/{len(symbols)}] Fetched StockScans details for {symbol}")
            except Exception as e:
                print(f"  [{i}/{len(symbols)}] Error fetching StockScans for {sym}: {e}")
    return results

def get_stockscans_common_stocks_data() -> dict:
    """Fetch live StockScans common-stocks (scan matches) list dynamically or fallback to local JSON file."""
    global STOCKSCANS_STATUS
    import json
    
    cookie = os.environ.get("STOCKSCANS_COOKIE", "")
    
    # 1. Attempt to fetch dynamically from API
    url = "https://www.stockscans.in/api/scans/stock/saved/common-stocks"
    headers = {
        "accept": "application/json",
        "content-type": "application/json",
        "cookie": cookie,
        "origin": "https://www.stockscans.in",
        "referer": "https://www.stockscans.in/scan-match",
        "user-agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, Gecko) Chrome/153.0.0.0 Safari/537.36"
    }
    payload = {"includePopular": True}
    print(f"Attempting to fetch live StockScans common stocks from {url}...")
    try:
        r = requests.post(url, headers=headers, json=payload, timeout=20)
        if r.status_code == 200:
            data = r.json()
            if "companies" in data and len(data["companies"]) > 0:
                print(f"Successfully fetched {len(data['companies'])} live StockScans companies!")
                STOCKSCANS_STATUS["status"] = "success"
                STOCKSCANS_STATUS["fetched_live"] = True
                STOCKSCANS_STATUS["message"] = "Fetched live successfully"
                return data
            else:
                print("Live response loaded but companies list was empty.")
                STOCKSCANS_STATUS["status"] = "empty_response"
                STOCKSCANS_STATUS["message"] = "Response returned empty companies list."
        else:
            print(f"Live API request failed with status code: {r.status_code}")
            if r.status_code in [401, 403]:
                STOCKSCANS_STATUS["status"] = "expired"
                STOCKSCANS_STATUS["message"] = f"StockScans session expired (HTTP {r.status_code}). Please update cookie."
            else:
                STOCKSCANS_STATUS["status"] = "failed"
                STOCKSCANS_STATUS["message"] = f"API returned error status code: {r.status_code}"
    except Exception as e:
        print(f"Error fetching live StockScans common stocks API from {url}: {e}")
        STOCKSCANS_STATUS["status"] = "failed"
        STOCKSCANS_STATUS["message"] = f"Connection error: {str(e)}"

    return {}

def load_scan_matched_symbols() -> list[str]:
    """Read symbols from StockScans live common-stocks data."""
    data = get_stockscans_common_stocks_data()
    companies = data.get("companies", [])
    symbols = []
    for c in companies:
        comp_id = c.get("companyId", "")
        symbol = comp_id.split(":")[1] if ":" in comp_id else comp_id
        if symbol:
            symbols.append(symbol.strip())
    return list(set(symbols))

def load_scan_matched_df(universe: pd.DataFrame, yfinance_data: dict[str, dict]) -> pd.DataFrame:
    """Load StockScans live common-stocks as a normalized DataFrame and enrich sector from Chartink or Yahoo Finance."""
    data = get_stockscans_common_stocks_data()
    companies = data.get("companies", [])
    rows = []
    for c in companies:
        comp_id = c.get("companyId", "")
        symbol = comp_id.split(":")[1] if ":" in comp_id else comp_id
        
        # 1. Try to get sector from Chartink universe first
        sector = "unknown"
        univ_match = universe[universe["symbol"] == symbol]
        if not univ_match.empty and pd.notna(univ_match.iloc[0].get("sector")):
            sector = univ_match.iloc[0]["sector"]
        
        # 2. Try to get sector from Yahoo Finance info second
        if sector == "unknown" or not sector:
            details = yfinance_data.get(symbol, {})
            info = details.get("info", {})
            if info and info.get("sector"):
                sector = info.get("sector")
                
        rows.append({
            "symbol": symbol,
            "company": c.get("Name", ""),
            "sector": sector or "unknown",
            "industry": c.get("Industry", ""),
            "marketcap_bucket": "smallcap" if c.get("Market Capitalization", 0) < 5000 else "midcap" if c.get("Market Capitalization", 0) < 20000 else "largecap",
            "marketcap_cr": c.get("Market Capitalization", 0.0),
            "close": c.get("Close Price", 0.0),
            "today_return_pct": c.get("Returns 1D", 0.0),
            "volume": 0.0,
            "Scans": c.get("Scans", 0)
        })
    return pd.DataFrame(rows)

