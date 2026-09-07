#!/usr/bin/env python3
"""
Scraper module for StockScans.in and Screener.in.
Handles actuals queries, PDF searches, ValuePickr JSON API fetches, and formatting tables to Markdown.
"""

from __future__ import annotations
import os
import re
import time
import requests
from bs4 import BeautifulSoup
from concurrent.futures import ThreadPoolExecutor, as_completed

GOOGLE_SEARCH_COUNTER = 0

def clean_company_name(name: str) -> str:
    """Clean company names for search engine queries."""
    if not name:
        return ""
    # Strip common legal suffixes
    suffixes = [
        r"\bLTD\b", r"\bLIMITED\b", r"\bINDIA\b", r"\bPVT\b", r"\bPRIVATE\b",
        r"\bCORP\b", r"\bCORPORATION\b", r"\bINDUSTRIES\b", r"\bENTERPRISES\b"
    ]
    cleaned = name.upper()
    for s in suffixes:
        cleaned = re.sub(s, "", cleaned)
    cleaned = re.sub(r"[^\w\s]", "", cleaned)
    return " ".join(cleaned.split()).strip()

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
            
    fundamentals_data = {}
    url = f"https://www.stockscans.in/api/company/fundamentals/{exchange}:{symbol}/{source}"
    try:
        r = requests.get(url, headers=headers, timeout=12)
        if r.status_code == 200:
            fundamentals_data = r.json()
    except Exception as e:
        print(f"⚠️ Error fetching fundamentals from StockScans for {symbol}: {e}")
        
    peers_list = []
    url = "https://www.stockscans.in/api/company/industry-peers"
    payload = {"companyIds": [f"{exchange}:{symbol}"], "limit": 6}
    try:
        r = requests.post(url, headers=headers, json=payload, timeout=12)
        if r.status_code == 200:
            peers_list = r.json().get("companies", [])
    except Exception as e:
        print(f"⚠️ Error fetching industry peers from StockScans for {symbol}: {e}")
        
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
    """Fetch fundamentals for multiple peer symbols in parallel."""
    results = {}
    if not peer_ids:
        return results
        
    cookie = os.environ.get("STOCKSCANS_COOKIE", "")
    headers = {
        "accept": "application/json",
        "content-type": "application/json",
        "cookie": cookie,
        "user-agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, Gecko) Chrome/148.0.0.0 Safari/537.36"
    }
    
    def fetch_single(company_id):
        try:
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
    """Format StockScans fundamentals, peers, and shareholding data into clean Markdown tables."""
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
    
    if yearly_data and len(yearly_data) > 1:
        headers = yearly_data[0]
        rows = yearly_data[1:]
        header_map = {h: i for i, h in enumerate(headers)}
        row_map = {r[header_map["Date"]]: r for r in rows if "Date" in header_map}
        
        available_years = sorted(list(row_map.keys()))
        years_to_show = available_years[-5:] if len(available_years) >= 5 else available_years
        
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
        
    peers = data.get("peers", [])
    card_details = data.get("card_details", {})
    if peers:
        peer_ids = [p["companyId"] for p in peers]
        peer_funds = fetch_peers_fundamentals_in_parallel(peer_ids)
        
        all_ids = [target_id] + peer_ids
        seen = set()
        dedup_ids = []
        for pid in all_ids:
            if pid not in seen:
                seen.add(pid)
                dedup_ids.append(pid)
                
        rows = []
        for pid in dedup_ids:
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

def fetch_stockscans_documents(symbol: str, exchange: str = "NSE") -> dict:
    """Fetch PDF documents (investor presentations, annual reports, concalls) from StockScans."""
    url = "https://www.stockscans.in/api/company/scans/documents"
    headers = {
        "accept": "application/json",
        "content-type": "application/json",
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, Gecko) Chrome/115.0.0.0 Safari/537.36"
    }
    payload = {"companyId": f"{exchange}:{symbol}"}
    
    pdf_map = {"ip_pdf": "", "ar_pdf": "", "concall_pdf": ""}
    try:
        print(f"📂 [StockScans API] Querying PDF reports/presentations for {symbol}...")
        r = requests.post(url, headers=headers, json=payload, timeout=12)
        if r.status_code == 200:
            data = r.json()
            docs = data.get("documents", [])
            
            for doc in docs:
                title = doc.get("title", "").upper()
                d_url = doc.get("url", "")
                if not d_url:
                    continue
                if not d_url.startswith("http"):
                    d_url = "https://www.stockscans.in" + d_url
                    
                if "PRESENTATION" in title and not pdf_map["ip_pdf"]:
                    pdf_map["ip_pdf"] = d_url
                elif "ANNUAL REPORT" in title and not pdf_map["ar_pdf"]:
                    pdf_map["ar_pdf"] = d_url
                elif ("CONCALL" in title or "TRANSCRIPT" in title) and not pdf_map["concall_pdf"]:
                    pdf_map["concall_pdf"] = d_url
    except Exception as e:
        print(f"⚠️ Error fetching StockScans documents for {symbol}: {e}")
    return pdf_map

def fetch_screener_documents(symbol: str) -> dict:
    """Fallback parser to scrape primary source PDF documents from Screener.in."""
    url = f"https://www.screener.in/company/{symbol}/consolidated/"
    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, Gecko) Chrome/115.0.0.0 Safari/537.36"
    }
    pdf_map = {"ip_pdf": "", "ar_pdf": "", "concall_pdf": ""}
    try:
        r = requests.get(url, headers=headers, timeout=15)
        if r.status_code == 200:
            soup = BeautifulSoup(r.text, "html.parser")
            
            for link in soup.find_all("a", href=True):
                href = link["href"]
                text = link.get_text().upper()
                
                if ".PDF" in href.upper() or "DOCUMENT" in href.lower() or "download" in href.lower():
                    if "PRESENTATION" in text and not pdf_map["ip_pdf"]:
                        pdf_map["ip_pdf"] = href
                    elif "ANNUAL REPORT" in text and not pdf_map["ar_pdf"]:
                        pdf_map["ar_pdf"] = href
                    elif ("CONCALL" in text or "TRANSCRIPT" in text or "EARNINGS CALL" in text) and not pdf_map["concall_pdf"]:
                        pdf_map["concall_pdf"] = href
                        
            # Normalize relative links
            for k, val in pdf_map.items():
                if val and val.startswith("/"):
                    pdf_map[k] = "https://www.screener.in" + val
    except Exception as e:
        print(f"⚠️ Screener parser failed for {symbol}: {e}")
    return pdf_map

def fetch_valuepickr_thread_url(company_name: str, symbol: str) -> tuple[str, int | None]:
    """Search for the official ValuePickr forum thread for the company with multi-query fallback."""
    cleaned = clean_company_name(company_name)
    candidates = []
    
    # Candidate 1: Stripped core name without generic sector/business words (e.g. "Syrma SGS Technology" -> "Syrma SGS")
    stripped = re.sub(
        r'\b(technology|technologies|industries|industry|enterprises|enterprise|solutions|solution|systems|system|india|international|holdings|holding|services|service|products|product|healthcare|pharmaceuticals|pharma)\b',
        '', cleaned, flags=re.IGNORECASE
    )
    stripped = re.sub(r'\s+', ' ', stripped).strip()
    if stripped and stripped.lower() != cleaned.lower() and len(stripped) >= 3:
        candidates.append(stripped)
        
    # Candidate 2: Full cleaned name
    if cleaned and cleaned not in candidates:
        candidates.append(cleaned)
        
    # Candidate 3: First distinctive words if multi-word
    words = [w for w in (stripped or cleaned).split() if len(w) >= 3]
    if len(words) > 1 and words[0] not in candidates:
        candidates.append(words[0])
        
    # Candidate 4: Symbol
    if symbol and len(symbol) >= 4 and symbol not in candidates:
        candidates.append(symbol)

    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, Gecko) Chrome/115.0.0.0 Safari/537.36",
        "Accept": "application/json"
    }
    
    for term in candidates:
        url = f"https://forum.valuepickr.com/search/query?term={term.replace(' ', '%20')}"
        try:
            print(f"🔍 [ValuePickr API] Querying thread search for: {term}")
            r = requests.get(url, headers=headers, timeout=12)
            if r.status_code == 200:
                data = r.json()
                topics = data.get("topics", [])
                if topics:
                    query_terms = [t.lower() for t in term.split() if len(t) >= 3]
                    best_topic = None
                    for t in topics:
                        t_title = t.get("title", "").lower()
                        t_slug = t.get("slug", "").lower()
                        if any(qt in t_title or qt in t_slug for qt in query_terms):
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
            print(f"⚠️ ValuePickr API search error on '{term}': {e}")
            
    return "https://forum.valuepickr.com/", None

def fetch_valuepickr_posts(topic_id: int) -> str:
    """Fetch the top 5 and bottom 5 posts from the ValuePickr thread."""
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
            
            selected_posts = []
            top_count = min(5, len(posts))
            for i in range(top_count):
                selected_posts.append((i + 1, posts[i]))
                
            if len(posts) > 5:
                bottom_start = max(5, len(posts) - 5)
                for i in range(bottom_start, len(posts)):
                    selected_posts.append((i + 1, posts[i]))
            
            compiled_text = ""
            for idx, post in selected_posts:
                username = post.get("username", "User")
                raw_cooked = post.get("cooked", "")
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

def fetch_ddg_search_results(query: str, limit: int = 5) -> list[dict]:
    """Search for results using Tavily Search API."""
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
