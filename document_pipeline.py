#!/usr/bin/env python3
"""
Document Pipeline Module
Handles downloading/extracting PDFs, scraping Substack/News content, 
filtering ValuePickr forum posts, and summarizing texts using DeepSeek.
"""

import os
import re
import time
import requests
from datetime import datetime, timezone, timedelta
from pathlib import Path
from bs4 import BeautifulSoup
from pypdf import PdfReader
import urllib.parse
import xml.etree.ElementTree as ET

def download_and_extract_pdf(url: str, doc_type: str, max_pages: int = 9999) -> str:
    """Download a PDF from a URL and extract text from its pages."""
    if not url or url.startswith("https://nseindia.com") or url.startswith("https://concall.in"):
        print(f"⚠️ No valid URL found on StockScans or Screener for {doc_type} (placeholder/empty: {url}). Skipping download.")
        return ""
        
    print(f"📥 [DOWNLOADING] Fetching {doc_type} PDF from: {url}")
    temp_dir = Path("scratch")
    temp_dir.mkdir(exist_ok=True)
    pdf_path = temp_dir / f"temp_{doc_type}.pdf"
    
    headers = {
        "user-agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, Gecko) Chrome/148.0.0.0 Safari/537.36"
    }
    
    try:
        response = requests.get(url, headers=headers, timeout=30)
        if response.status_code == 200:
            with open(pdf_path, "wb") as f:
                f.write(response.content)
            print(f"✅ Successfully downloaded {doc_type} PDF.")
            
            reader = PdfReader(pdf_path)
            total_pages = len(reader.pages)
            pages_to_read = min(total_pages, max_pages)
            print(f"📖 Parsing {pages_to_read} of {total_pages} pages from {doc_type}...")
            
            text_content = []
            for i in range(pages_to_read):
                page_text = reader.pages[i].extract_text()
                if page_text:
                    text_content.append(f"--- PAGE {i+1} ---\n{page_text}")
                    
            extracted_text = "\n\n".join(text_content).strip()
            if pdf_path.exists():
                pdf_path.unlink()
            return extracted_text
        else:
            print(f"⚠️ Failed to download {doc_type} PDF (HTTP status {response.status_code})")
            return ""
    except Exception as e:
        print(f"⚠️ Error downloading or parsing {doc_type} PDF: {e}")
        if pdf_path.exists():
            pdf_path.unlink()
        return ""

def scrape_full_substack_content(url: str) -> str:
    """Scrape the full text content of a Substack article."""
    if not url or "substack.com" not in url:
        return ""
    print(f"🕸️ [SCRAPING] Fetching full Substack article: {url}")
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, Gecko) Chrome/120.0.0.0 Safari/537.36"
    }
    try:
        r = requests.get(url, headers=headers, timeout=15)
        if r.status_code == 200:
            soup = BeautifulSoup(r.text, "html.parser")
            for element in soup(["script", "style", "nav", "footer", "header"]):
                element.decompose()
            text = soup.get_text(separator="\n")
            lines = [l.strip() for l in text.splitlines() if l.strip()]
            cleaned_text = "\n".join(lines)
            return cleaned_text[:8000]
    except Exception as e:
        print(f"⚠️ Failed to scrape Substack content from {url}: {e}")
    return ""

def scrape_full_news_content(google_news_url: str) -> str:
    """Follow Google News redirect link and scrape actual news page body."""
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, Gecko) Chrome/120.0.0.0 Safari/537.36"
    }
    try:
        print(f"🕸️ [NEWS SCRAPING] Resolving and fetching article: {google_news_url}")
        r = requests.get(google_news_url, headers=headers, timeout=10, allow_redirects=True)
        if r.status_code == 200:
            soup = BeautifulSoup(r.text, "html.parser")
            for element in soup(["script", "style", "nav", "footer", "header", "aside", "form"]):
                element.decompose()
            text = soup.get_text(separator="\n")
            lines = [l.strip() for l in text.splitlines() if l.strip()]
            cleaned_text = "\n".join(lines)
            return cleaned_text[:3500]
    except Exception as e:
        print(f"⚠️ Failed to scrape news content from {google_news_url}: {e}")
    return ""

def get_google_news_rss(company_name: str, limit: int = 5) -> str:
    """Fetch search results from Google News RSS, follow redirects, and scrape page content."""
    query = urllib.parse.quote(f'"{company_name}" stock')
    rss_url = f"https://news.google.com/rss/search?q={query}&hl=en-IN&gl=IN&ceid=IN:en"
    
    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, Gecko) Chrome/120.0.0.0 Safari/537.36"
    }
    
    print(f"📡 [Google News RSS] Fetching news feed for: {company_name}...")
    try:
        response = requests.get(rss_url, headers=headers, timeout=15)
        if response.status_code != 200:
            return ""
        root = ET.fromstring(response.content)
        articles = []
        for idx, item in enumerate(root.findall(".//item")[:limit]):
            title = item.find("title").text
            link = item.find("link").text
            pub_date = item.find("pubDate").text
            
            scraped_body = scrape_full_news_content(link)
            if scraped_body:
                articles.append(f"### News Article #{idx+1}: {title}\nDate: {pub_date}\nLink: {link}\nContent:\n{scraped_body}\n")
            else:
                desc = item.find("description").text or ""
                soup = BeautifulSoup(desc, "html.parser")
                clean_desc = " ".join(soup.get_text(separator=" ").split())[:300]
                articles.append(f"### News Article #{idx+1}: {title}\nDate: {pub_date}\nLink: {link}\nContent Fallback (Snippet):\n{clean_desc}\n")
                
        return "\n\n".join(articles)
    except Exception as e:
        print(f"⚠️ Error fetching Google News: {e}")
        return ""

def fetch_valuepickr_posts_latest_1_year(topic_id: int) -> str:
    """Fetch ValuePickr thread posts filtered for the latest 1 year."""
    url = f"https://forum.valuepickr.com/t/{topic_id}.json"
    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, Gecko) Chrome/115.0.0.0 Safari/537.36",
        "Accept": "application/json"
    }
    try:
        print(f"📖 [ValuePickr API] Fetching posts for topic ID: {topic_id} (filtering latest 1 year)...")
        r = requests.get(url, headers=headers, timeout=15)
        if r.status_code == 200:
            data = r.json()
            post_stream = data.get("post_stream", {})
            posts = post_stream.get("posts", [])
            if not posts:
                return ""
            
            now = datetime.now(timezone.utc)
            one_year_ago = now - timedelta(days=365)
            
            recent_posts = []
            for i, post in enumerate(posts):
                created_str = post.get("created_at")
                if created_str:
                    try:
                        created_dt = datetime.fromisoformat(created_str.replace("Z", "+00:00"))
                        if created_dt >= one_year_ago:
                            recent_posts.append((i + 1, post))
                    except Exception:
                        pass
                        
            if not recent_posts:
                print("ℹ️ No posts in last 1 year. Falling back to the latest 10 posts in the thread.")
                recent_start = max(0, len(posts) - 10)
                for i in range(recent_start, len(posts)):
                    recent_posts.append((i + 1, posts[i]))
            
            compiled_text = ""
            for idx, post in recent_posts:
                username = post.get("username", "User")
                raw_cooked = post.get("cooked", "")
                clean_text = re.sub(r'<[^>]*>', ' ', raw_cooked)
                clean_text = re.sub(r'\s+', ' ', clean_text).strip()
                if len(clean_text) > 1500:
                    clean_text = clean_text[:1500] + "... (truncated)"
                compiled_text += f"Post #{idx} by @{username}:\n{clean_text}\n\n"
            return compiled_text.strip()
    except Exception as e:
        print(f"⚠️ ValuePickr fetch failed: {e}")
    return ""

def log_llm_call(model: str, role: str, prompt: str, output: str):
    """Log LLM prompt and response to a structured file for debugging/verification."""
    logs_dir = Path("logs")
    logs_dir.mkdir(exist_ok=True)
    log_file = logs_dir / "custom_report_generation_flow.log"
    with open(log_file, "a", encoding="utf-8") as lf:
        lf.write(f"\n{'='*80}\n")
        lf.write(f"🕒 TIMESTAMP: {datetime.now().isoformat()}\n")
        lf.write(f"🤖 MODEL: {model}\n")
        lf.write(f"🎯 ROLE/STAGE: {role}\n")
        lf.write(f"{'-'*80}\n")
        lf.write(f"📝 PROMPT:\n{prompt}\n")
        lf.write(f"{'-'*80}\n")
        lf.write(f"📥 RESPONSE:\n{output}\n")
        lf.write(f"{'='*80}\n\n")

def get_cached_document(symbol: str, doc_type: str, date_key: str, url: str) -> str:
    """
    Check if a cached summary exists for a given symbol, document type, date key, and URL.
    Returns the summary string if found and valid, otherwise None.
    """
    if not url or "nseindia.com" in url or "concall.in" in url:
        return None
        
    comp_dir = Path("outputs") / "intermediate_summaries" / symbol
    metadata_path = comp_dir / "cache_metadata.json"
    
    if not metadata_path.exists():
        return None
        
    try:
        import json
        with open(metadata_path, "r", encoding="utf-8") as f:
            meta = json.load(f)
            
        doc_key = doc_type.lower().replace(" ", "_")
        doc_meta = meta.get(doc_key)
        if not doc_meta:
            return None
            
        # Check if date_key and URL match
        if doc_meta.get("date_key") == date_key and doc_meta.get("url") == url:
            filename = doc_meta.get("filename")
            if filename:
                file_path = comp_dir / filename
                if file_path.exists():
                    with open(file_path, "r", encoding="utf-8") as f_md:
                        content = f_md.read().strip()
                        if content and len(content) > 100:
                            print(f"🎯 [CACHE HIT] Loaded cached {doc_type} summary for {symbol} ({date_key})")
                            return content
    except Exception as e:
        print(f"⚠️ Error reading cache for {symbol} ({doc_type}): {e}")
        
    return None

def save_document_to_cache(symbol: str, doc_type: str, date_key: str, url: str, content: str):
    """
    Save the document summary text to a markdown file and update the cache_metadata.json file.
    """
    if not content or len(content.strip()) < 100:
        print(f"⚠️ Refusing to cache empty/short summary for {symbol} ({doc_type})")
        return
        
    if not url or "nseindia.com" in url or "concall.in" in url:
        return
        
    try:
        import json
        comp_dir = Path("outputs") / "intermediate_summaries" / symbol
        comp_dir.mkdir(parents=True, exist_ok=True)
        
        doc_key = doc_type.lower().replace(" ", "_")
        filename = f"{doc_key}_{date_key}.md"
        
        # Save the markdown summary content
        file_path = comp_dir / filename
        with open(file_path, "w", encoding="utf-8") as f_md:
            f_md.write(content)
            
        # Load and update the metadata JSON
        metadata_path = comp_dir / "cache_metadata.json"
        meta = {}
        if metadata_path.exists():
            try:
                with open(metadata_path, "r", encoding="utf-8") as f:
                    meta = json.load(f)
            except Exception:
                meta = {}
                
        meta[doc_key] = {
            "date_key": date_key,
            "url": url,
            "filename": filename,
            "cached_at": datetime.now().isoformat()
        }
        
        with open(metadata_path, "w", encoding="utf-8") as f:
            json.dump(meta, f, indent=2)
            
        print(f"💾 [CACHE SAVE] Cached {doc_type} summary for {symbol} ({date_key})")
    except Exception as e:
        print(f"⚠️ Error writing cache for {symbol} ({doc_type}): {e}")

# Default free models pool for 3-way rotation
DEFAULT_FREE_MODELS = [
    "minimax/minimax-m3:free",
    "z-ai/glm-5.2:free",
    "nvidia/nemotron-3-ultra-550b-a55b:free"
]

# Track rate-limit cooldown timestamps: {model_name: timestamp_when_ready}
_MODEL_COOLDOWNS = {}

def mark_model_cooldown(model: str, cooldown_seconds: int = 45):
    """Mark a model as temporarily cooling down due to rate limit (429) or busy error (503)."""
    _MODEL_COOLDOWNS[model] = time.time() + cooldown_seconds

def is_model_cooling_down(model: str) -> bool:
    """Check if a model is currently cooling down."""
    ready_time = _MODEL_COOLDOWNS.get(model, 0)
    return time.time() < ready_time

def get_model_pool(primary_model: str = None) -> list[str]:
    """
    Return an ordered, deduplicated list of models ensuring all 3 free models are present:
    [primary] + [FALLBACK_MODELS] + [DEFAULT_FREE_MODELS]
    """
    models = []
    if primary_model and primary_model.strip():
        models.append(primary_model.strip())
    
    fallback_env = os.environ.get("FALLBACK_MODELS", "")
    if fallback_env:
        for m in fallback_env.split(","):
            m_clean = m.strip()
            if m_clean and m_clean not in models:
                models.append(m_clean)
                
    for default_m in DEFAULT_FREE_MODELS:
        if default_m not in models:
            models.append(default_m)
            
    return models

def _build_summarization_prompt(text: str, doc_type: str) -> str:
    """Build task-specific summarization prompt."""
    if doc_type == "Investor Presentation":
        return (
            "You are an expert sell-side equity research analyst. Your task is to extract all critical slides and content from this Investor Presentation transcript.\n\n"
            "FIELDS TO EXTRACT EXCLUSIVELY:\n"
            "- Core Product Segments & Launches (new fragrances, elixirs, active-ingredient cosmetics)\n"
            "- Quantitative metrics: Growth trajectory, market shares, handler shares, category ARR, and margin contribution\n"
            "- Future Growth Adjacencies: Details on fragrance entry, oral beauty/nutrition M&A, offline channel additions\n"
            "- Visualise and build a clear breakdown of revenue mix by brand/division based on presentation data\n\n"
            "CRITICAL RULES:\n"
            "- Restrict extraction ONLY to the provided text. Never assume or extrapolate.\n"
            "- Present output as structured, extremely dense, data-driven bullet points.\n\n"
            f"Text to analyze:\n{text}"
        )
    elif doc_type == "Annual Report":
        return (
            "You are a world class equity analyst specialising in annual report forensics and business quality assessment.\n\n"
            "Analyze this Annual Report and extract the following — be concise, insight-dense, no fluff:\n\n"
            "MANAGEMENT LETTERS:\n"
            "- Key themes from MD/Chairman/CEO/CFO letters\n"
            "- Stated opportunities, risks, and strategic priorities\n"
            "- Walk-the-talk check: compare stated FY guidance vs actual delivery\n\n"
            "BUSINESS & STRATEGY:\n"
            "- Core business segments and revenue mix\n"
            "- Any new segments, products, geographies added this year\n"
            "- Capex done vs planned — what got commissioned, what is pending\n\n"
            "FINANCIAL QUALITY CHECK:\n"
            "- Revenue & PAT growth (3Y trend)\n"
            "- EBITDA margin trend — expanding or contracting?\n"
            "- CFO vs PAT — is profit converting to cash?\n"
            "- Working capital movement — receivables, inventory days\n"
            "- ROCE/ROE trend — improving or deteriorating?\n"
            "- Debt: net debt position, D/E, interest coverage\n\n"
            "FORENSIC FLAGS:\n"
            "- Related party transactions — size, nature, any suspicious flows\n"
            "- Contingent liabilities vs networth (flag if >10%)\n"
            "- Miscellaneous expenses as % of revenue (flag if >3%)\n"
            "- Auditor opinion — Qualified or Unqualified?\n"
            "- Key Audit Matters — any red flags?\n"
            "- CARO report — anything concerning?\n"
            "- Accounting policy changes — aggressive or conservative?\n\n"
            "GOVERNANCE:\n"
            "- Management remuneration as % of revenue\n"
            "- KMP changes or resignations\n"
            "- Promoter pledging or stake changes\n\n"
            "OUTPUT FORMAT:\n"
            "- Green / Yellow / Red flag checklist on each parameter\n"
            "- Final accounting quality score: Good / Average / Bad\n\n"
            "CRITICAL RULES:\n"
            "- Keep output: bullet-heavy, no number repetition, every point must add analytical value.\n"
            "- Limit analysis strictly to the facts present in the text. Do not invent details.\n\n"
            f"Text to analyze:\n{text}"
        )
    elif doc_type == "Concall Transcript":
        return (
            "You are a financial research assistant analyzing a company's earnings conference call transcript.\n\n"
            "EXTRACT EXCLUSIVELY:\n"
            "- Management Commentary: Key statements from CEO, CFO, and leadership\n"
            "- Tone, clarity, and confidence of responses\n"
            "- Focus Areas: Product launches, channel margins, inventory levels (Project Neev, direct distribution)\n"
            "- Future Outlook & Guidance: Margins (EBITDA bridge components, mix shift, operating leverage, seasonality, ESOP reversal), volume growth, CAPEX, M&A integration (Fluence Pharma, BTM/Reginald)\n"
            "- Q&A Dynamics: Analyst queries vs management answers. Highlight areas where management was evasive or highly confident\n\n"
            "CRITICAL RULES:\n"
            "- Strictly stick to the text. Do not invent details.\n\n"
            f"Text to analyze:\n{text}"
        )
    elif doc_type == "Substack Research Articles":
        return (
            "You are a buy-side investment analyst. Synthesize these third-party research articles into a clear, critical thesis summary.\n\n"
            "EXTRACT EXCLUSIVELY:\n"
            "- Core investment thesis: Bull vs Bear arguments\n"
            "- Key assumptions: Working capital needs, offline expansion (store count), ad-spend efficiency, CAC dynamics, lock-in exits (Sofina, etc.)\n"
            "- Unique Insights: Exits patterns (venture backlog cohort events), channel mix efficiency, RAG/data extraction complexities\n"
            "- Financial frameworks: Valuation methodologies, target multiples, scenario assumptions\n"
            "- Risks highlighted: Attention inflation, acquisition integration, Nykaa/competitor overlaps, working capital traps\n\n"
            "CRITICAL RULES:\n"
            "- Analyze and present objectively. Highlight any biases in the articles.\n\n"
            f"Text to analyze:\n{text}"
        )
    elif doc_type == "ValuePickr Forum Posts":
        return (
            "You are analyzing community discussions from an active investor forum (ValuePickr).\n\n"
            "EXTRACT EXCLUSIVELY:\n"
            "- Consensus vs Variant View among retail/individual investors\n"
            "- Events the community is watching (results, capex, approvals, audits)\n"
            "- Red flags being tracked\n"
            "- Milestones that would change sentiment\n\n"
            "NOISE TO IGNORE\n"
            "- Momentum/price-based chatter — exclude\n"
            "- Repetitive thesis restating — exclude\n"
            "- Unverified rumours — flag separately, do not include in main analysis\n\n"
            "OUTPUT: Structured bullets only. Label each insight as: Verified / Needs Verification / Rumour. "
            "Prioritise insights not available from company filings.\n\n"
            f"Text to analyze:\n{text}"
        )
    else:
        return (
            f"You are an expert equity research analyst. Summarize the following {doc_type} text. "
            f"Extract all critical data points, revenue/profit guidance, capital expenditure (CAPEX) plans, product segments, key risks, promoter/management commentary, and financial performance details. "
            f"Ensure the summary is extremely dense, data-driven, structured, and free of fluff. Keep the summary under 2000 words.\n\n"
            f"Text to summarize:\n{text}"
        )

def summarize_text_via_deepseek(text: str, doc_type: str) -> str:
    """
    Summarize document text with automatic 3-way load balancing across free models
    (minimax/minimax-m3:free, z-ai/glm-5.2:free, nvidia/nemotron-3-ultra-550b-a55b:free)
    and graceful failover on HTTP 429/503/400.
    """
    if not text or not text.strip() or "No extracted text available" in text:
        return f"No content available to summarize for {doc_type}."
        
    api_key = os.environ.get("OPENROUTER_API_KEY")
    if not api_key:
        raise KeyError("Required environment variable 'OPENROUTER_API_KEY' is missing.")
        
    url = "https://openrouter.ai/api/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://github.com/monitranjan/multibagger-scanner",
        "X-Title": "Multibagger Scanner"
    }
    
    # 3-way load balancing distribution across document types
    preferred_by_type = {
        "Annual Report": "minimax/minimax-m3:free",               # 1M context handles massive PDFs
        "Concall Transcript": "z-ai/glm-5.2:free",                 # Excellent reasoning for Q&A
        "Investor Presentation": "nvidia/nemotron-3-ultra-550b-a55b:free", # High capacity for presentation data
        "Substack Research Articles": "minimax/minimax-m3:free",   # Long context for multi-article articles
        "Google News Articles": "z-ai/glm-5.2:free",               # Fast synthesis for news items
        "ValuePickr Forum Posts": "nvidia/nemotron-3-ultra-550b-a55b:free", # Forum sentiment analysis
    }
    
    primary_env = os.environ.get("SUMMARIZATION_MODEL", "").strip()
    preferred_model = preferred_by_type.get(doc_type, primary_env or "minimax/minimax-m3:free")
    
    all_models = get_model_pool(preferred_model)
    # Order candidates: ready models first (starting with preferred), then cooling-down models as last resort
    ready_models = [m for m in all_models if not is_model_cooling_down(m)]
    cooling_models = [m for m in all_models if m not in ready_models]
    candidate_models = ready_models + cooling_models

    for model_idx, model in enumerate(candidate_models):
        for attempt in range(1, 3):
            # Guard against HTTP 400 Context Overflow: truncate for models without 1M context
            current_text = text
            if "minimax" not in model.lower() and len(current_text) > 250000:
                current_text = current_text[:250000] + "\n... (truncated to fit model context window)"
                
            prompt = _build_summarization_prompt(current_text, doc_type)
            payload = {
                "model": model,
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0.2,
            }
            
            print(f"🤖 [SUMMARIZATION] Requesting {model} for {doc_type} (Attempt {attempt}/2, len: {len(current_text)} chars)...")
            try:
                response = requests.post(url, json=payload, headers=headers, timeout=120)
                if response.status_code == 200:
                    res_json = response.json()
                    choices = res_json.get("choices", [])
                    if choices:
                        summary = choices[0].get("message", {}).get("content", "").strip()
                        if summary:
                            print(f"✅ Successfully summarized {doc_type} via {model} (length: {len(summary)} chars).")
                            log_llm_call(model, f"Document Summarization - {doc_type}", prompt, summary)
                            return summary
                elif response.status_code == 429:
                    error_msg = response.text[:200]
                    print(f"⚠️ [HTTP 429] Rate limited on {model} for {doc_type}: {error_msg}")
                    mark_model_cooldown(model, 45)
                    sleep_s = 5 * attempt
                    print(f"⏳ Cooling down {model} for 45s (sleeping {sleep_s}s before next attempt/fallback)...")
                    time.sleep(sleep_s)
                elif response.status_code in (502, 503, 504):
                    error_msg = response.text[:200]
                    print(f"⚠️ [HTTP {response.status_code}] Provider capacity issue for {model}: {error_msg}")
                    mark_model_cooldown(model, 30)
                    time.sleep(3)
                elif response.status_code == 400:
                    error_msg = response.text[:200]
                    print(f"⚠️ [HTTP 400] Bad Request / Context overflow on {model}: {error_msg}")
                    break  # Switch to next model immediately
                else:
                    error_msg = response.text[:200]
                    print(f"⚠️ [HTTP {response.status_code}] OpenRouter returned error for {model}: {error_msg}")
                    time.sleep(2)
            except Exception as e:
                print(f"⚠️ Exception during summarization of {doc_type} with {model}: {e}")
                time.sleep(2)

    print(f"⚠️ All models failed for {doc_type}. Using raw/truncated text fallback.")
    return text[:8000] + "\n... (truncated raw text fallback)"


def scrape_screener_ratios(symbol: str) -> dict:
    """Scrape actual fundamental ratios and shareholding pattern details directly from Screener's public page."""
    url = f"https://www.screener.in/company/{symbol}/consolidated/"
    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, Gecko) Chrome/120.0.0.0 Safari/537.36"
    }
    ratios = {}
    try:
        r = requests.get(url, headers=headers, timeout=15)
        if r.status_code != 200:
            url = f"https://www.screener.in/company/{symbol}/"
            r = requests.get(url, headers=headers, timeout=15)
            
        if r.status_code == 200:
            soup = BeautifulSoup(r.text, "html.parser")
            
            # 1. Parse top warehouse ratios
            warehouse = soup.find("div", {"id": "top-ratios"})
            if warehouse:
                for li in warehouse.find_all("li"):
                    name_span = li.find("span", {"class": "name"})
                    val_span = li.find("span", {"class": "value"})
                    if name_span and val_span:
                        name = name_span.text.strip().replace("\n", "").replace("  ", " ")
                        val_text = val_span.text.strip()
                        # Clean value text
                        val_num = re.sub(r"[^\d\.-]", "", val_text)
                        try:
                            ratios[name] = float(val_num)
                        except ValueError:
                            ratios[name] = val_text
                            
            # 2. Parse Shareholding Pattern latest quarter
            shp_section = soup.find("section", {"id": "shareholding"})
            if shp_section:
                table = shp_section.find("table")
                if table:
                    rows = table.find_all("tr")
                    if len(rows) > 1:
                        headers_row = [th.text.strip() for th in rows[0].find_all("th") if th.text.strip()]
                        if not headers_row:
                            headers_row = [td.text.strip() for td in rows[0].find_all("td") if td.text.strip()]
                        
                        latest_row = rows[-1]
                        cols = [td.text.strip() for td in latest_row.find_all("td")]
                        
                        # Zip headers and cols to get holding percentages
                        for h, c in zip(headers_row[1:], cols[1:]):
                            clean_c = re.sub(r"[^\d\.-]", "", c)
                            try:
                                ratios[f"SH_{h}"] = float(clean_c)
                            except ValueError:
                                pass
    except Exception as e:
        print(f"⚠️ Screener scraper failed for {symbol}: {e}")
    return ratios


def fetch_screener_peers(symbol: str) -> str:
    """Fetch and parse peer comparison table from Screener's internal peer API."""
    search_url = f"https://www.screener.in/api/company/search/?q={symbol}"
    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, Gecko) Chrome/120.0.0.0 Safari/537.36"
    }
    try:
        r_search = requests.get(search_url, headers=headers, timeout=10)
        if r_search.status_code == 200:
            matches = r_search.json()
            if matches:
                company_id = matches[0]["id"]
                peers_url = f"https://www.screener.in/api/company/{company_id}/peers/"
                r_peers = requests.get(peers_url, headers=headers, timeout=10)
                if r_peers.status_code == 200:
                    soup = BeautifulSoup(r_peers.text, "html.parser")
                    table = soup.find("table")
                    if table:
                        headers_row = []
                        for th in table.find_all("th"):
                            h_text = th.text.strip().replace("\n", "").replace("  ", " ")
                            h_text = re.sub(r"\s+Rs\..*|\s+%.*|\s+Cr\..*", "", h_text)
                            headers_row.append(h_text.strip())
                        
                        rows = []
                        tbody = table.find("tbody")
                        if tbody:
                            for tr in tbody.find_all("tr"):
                                cols = [td.text.strip().replace("\n", "").replace("  ", " ") for td in tr.find_all("td")]
                                if cols and len(cols) == len(headers_row) and not any("median" in c.lower() for c in cols):
                                    rows.append(cols)
                        
                        if headers_row and rows:
                            md_lines = []
                            md_lines.append("| " + " | ".join(headers_row) + " |")
                            md_lines.append("| " + " | ".join([":---" for _ in headers_row]) + " |")
                            for r in rows:
                                md_lines.append("| " + " | ".join(r) + " |")
                            return "\n".join(md_lines)
    except Exception as e:
        print(f"⚠️ Error fetching Screener peers for {symbol}: {e}")
    return ""


def scrape_screener_financial_tables(symbol: str) -> dict:
    """Scrape and parse Profit & Loss, Balance Sheet, Cash Flow, and Ratios from Screener's public page."""
    url = f"https://www.screener.in/company/{symbol}/consolidated/"
    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, Gecko) Chrome/120.0.0.0 Safari/537.36"
    }
    
    formatted = {
        "income_statement": "",
        "balance_sheet": "",
        "cash_flow_ratios": "",
        "shareholding_table": ""
    }
    
    try:
        r = requests.get(url, headers=headers, timeout=15)
        if r.status_code != 200:
            url = f"https://www.screener.in/company/{symbol}/"
            r = requests.get(url, headers=headers, timeout=15)
            
        if r.status_code == 200:
            soup = BeautifulSoup(r.text, "html.parser")
            
            def parse_screener_section(section_id, row_mappings):
                section = soup.find("section", {"id": section_id})
                if not section:
                    return None
                table = section.find("table")
                if not table:
                    return None
                
                # Extract headers (which are dates/years)
                # Keep empty first header to preserve column alignment with row values
                headers = [th.text.strip().replace("\n", "").replace("  ", " ") for th in table.find_all("th")]
                if not headers or len([h for h in headers if h]) == 0:
                    headers = [td.text.strip().replace("\n", "").replace("  ", " ") for td in table.find_all("td")]
                
                # Extract row data
                rows = {}
                tbody = table.find("tbody")
                tr_list = tbody.find_all("tr") if tbody else table.find_all("tr")
                for tr in tr_list:
                    if "comment" in tr.get("class", []):
                        continue
                    cols = [td.text.strip().replace("\n", "").replace("  ", " ") for td in tr.find_all("td")]
                    if cols:
                        row_name = cols[0].lower().replace("+", "").replace(" ", " ").strip()
                        rows[row_name] = cols[1:]
                
                # Select the years to display (latest 5 years)
                years_headers = headers[1:]
                display_indices = list(range(len(years_headers)))
                if len(years_headers) > 5:
                    display_indices = list(range(len(years_headers) - 5, len(years_headers)))
                    
                selected_years = [years_headers[i] for i in display_indices]
                
                # Build MD table
                md_hdr = "| Particulars | " + " | ".join(selected_years) + " |"
                md_sep = "|:---| " + " | ".join(["---:"] * len(selected_years)) + " |"
                md_rows = []
                
                for scr_key, label in row_mappings.items():
                    # Find matching row name
                    matched_key = next((k for k in rows.keys() if scr_key.lower() in k.lower()), None)
                    if matched_key:
                        cells = [rows[matched_key][i] if i < len(rows[matched_key]) else "-" for i in display_indices]
                        md_rows.append(f"| {label} | " + " | ".join(cells) + " |")
                        
                if md_rows:
                    return "\n".join([md_hdr, md_sep] + md_rows)
                return None

            # 1. Parse Income Statement
            pl_map = {
                "sales": "Revenue",
                "operating profit": "EBITDA",
                "opm": "EBITDA Margin%",
                "other income": "Other Income",
                "interest": "Interest",
                "depreciation": "Depreciation",
                "profit before tax": "PBT",
                "tax": "Tax Rate%",
                "net profit": "PAT",
                "eps in rs": "EPS"
            }
            inc_md = parse_screener_section("profit-loss", pl_map)
            if inc_md:
                formatted["income_statement"] = inc_md
                
            # 2. Parse Balance Sheet
            bs_map = {
                "equity capital": "Equity Capital",
                "reserves": "Reserves",
                "borrowings": "Borrowings",
                "other liabilities": "Other Liabilities",
                "total liabilities": "Total Liabilities",
                "fixed assets": "Fixed Assets",
                "cwip": "CWIP",
                "investments": "Investments",
                "other assets": "Other Assets",
                "total assets": "Total Assets"
            }
            bs_md = parse_screener_section("balance-sheet", bs_map)
            if bs_md:
                formatted["balance_sheet"] = bs_md
                
            # 3. Parse Cash Flow
            cf_map = {
                "cash from operating activity": "CFO",
                "cash from investing activity": "CFI",
                "cash from financing activity": "CFF",
                "net cash flow": "Net Cash Flow"
            }
            # We can extract the cash flow rows
            cf_section = soup.find("section", {"id": "cash-flow"})
            cf_table = cf_section.find("table") if cf_section else None
            cf_rows = {}
            cf_years = []
            if cf_table:
                cf_headers = [th.text.strip() for th in cf_table.find_all("th") if th.text.strip()]
                cf_years = cf_headers[1:]
                for tr in (cf_table.find("tbody").find_all("tr") if cf_table.find("tbody") else cf_table.find_all("tr")):
                    cols = [td.text.strip().replace("\n", "").replace("  ", " ") for td in tr.find_all("td")]
                    if cols:
                        row_name = cols[0].lower().replace("+", "").replace(" ", " ").strip()
                        cf_rows[row_name] = cols[1:]
            
            # Parse ratios rows to merge
            ratios_section = soup.find("section", {"id": "ratios"})
            ratios_table = ratios_section.find("table") if ratios_section else None
            ratio_rows = {}
            if ratios_table:
                for tr in (ratios_table.find("tbody").find_all("tr") if ratios_table.find("tbody") else ratios_table.find_all("tr")):
                    cols = [td.text.strip().replace("\n", "").replace("  ", " ") for td in tr.find_all("td")]
                    if cols:
                        row_name = cols[0].lower().replace("+", "").replace(" ", " ").strip()
                        ratio_rows[row_name] = cols[1:]
                        
            # Merge both Cash Flow and Ratios
            if cf_years:
                display_indices = list(range(len(cf_years)))
                if len(cf_years) > 5:
                    display_indices = list(range(len(cf_years) - 5, len(cf_years)))
                selected_years = [cf_years[i] for i in display_indices]
                
                cf_md_hdr = "| Particulars | " + " | ".join(selected_years) + " |"
                cf_md_sep = "|:---| " + " | ".join(["---:"] * len(selected_years)) + " |"
                cf_md_rows = []
                
                # Cash Flow rows
                for scr_key, label in cf_map.items():
                    matched_key = next((k for k in cf_rows.keys() if scr_key.lower() in k.lower()), None)
                    if matched_key:
                        cells = [cf_rows[matched_key][i] if i < len(cf_rows[matched_key]) else "-" for i in display_indices]
                        cf_md_rows.append(f"| {label} | " + " | ".join(cells) + " |")
                        
                # Ratio rows
                ratio_map_to_merge = {
                    "debtor days": "Debtor Days",
                    "inventory days": "Inventory Days",
                    "days payable": "Days Payable",
                    "cash conversion cycle": "Cash Conversion Cycle",
                    "roce": "ROCE%"
                }
                for scr_key, label in ratio_map_to_merge.items():
                    matched_key = next((k for k in ratio_rows.keys() if scr_key.lower() in k.lower()), None)
                    if matched_key:
                        cells = [ratio_rows[matched_key][i] if i < len(ratio_rows[matched_key]) else "-" for i in display_indices]
                        cf_md_rows.append(f"| {label} | " + " | ".join(cells) + " |")
                        
                if cf_md_rows:
                    formatted["cash_flow_ratios"] = "\n".join([cf_md_hdr, cf_md_sep] + cf_md_rows)
                    
            # 4. Parse Shareholding Pattern
            sh_section = soup.find("section", {"id": "shareholding"})
            sh_table = sh_section.find("table") if sh_section else None
            if sh_table:
                sh_headers = [th.text.strip().replace("\n", "").replace("  ", " ") for th in sh_table.find_all("th") if th.text.strip()]
                sh_rows = []
                for tr in (sh_table.find("tbody").find_all("tr") if sh_table.find("tbody") else sh_table.find_all("tr")):
                    cols = [td.text.strip().replace("\n", "").replace("  ", " ") for td in tr.find_all("td")]
                    if cols:
                        sh_rows.append(cols)
                if sh_headers and sh_rows:
                    md_lines = []
                    md_lines.append("| " + " | ".join(sh_headers) + " |")
                    md_lines.append("| " + " | ".join([":---" for _ in sh_headers]) + " |")
                    for r in sh_rows:
                        md_lines.append("| " + " | ".join(r) + " |")
                    formatted["shareholding_table"] = "\n".join(md_lines)
                    
    except Exception as e:
        print(f"⚠️ Error scraping financial tables for {symbol}: {e}")
    return formatted


SECTOR_VALUATION_MATRIX = {
    "Banks": {
        "ratios": "Price to Book (P/B), ROE",
        "rationale": "Reflects capital efficiency & core capital base."
    },
    "NBFCs / Housing Finance": {
        "ratios": "P/B, P/E",
        "rationale": "Captures leverage + earnings predictability."
    },
    "Life Insurance": {
        "ratios": "Embedded Value (EV), P/EV",
        "rationale": "EV = present value of future profits + net worth."
    },
    "General Insurance": {
        "ratios": "Price to GWP, P/B",
        "rationale": "Premium scale & solvency drive value."
    },
    "Asset Management": {
        "ratios": "P/E, % of AUM",
        "rationale": "Fees tied to AUM; scalable profits."
    },
    "Capital Markets": {
        "ratios": "EV/EBITDA, P/E",
        "rationale": "Stable transaction-driven cash flows."
    },
    "IT Services": {
        "ratios": "P/E, EV/EBITDA",
        "rationale": "Earnings growth key in asset-light model."
    },
    "FMCG": {
        "ratios": "P/E, EV/EBITDA",
        "rationale": "High brand value, steady margins."
    },
    "Consumer Durables": {
        "ratios": "P/E, EV/Sales",
        "rationale": "Brand premium + revenue growth matters."
    },
    "Automobiles": {
        "ratios": "EV/EBITDA, P/E",
        "rationale": "Margins + product mix captured well."
    },
    "Auto Ancillaries": {
        "ratios": "EV/EBITDA, P/E",
        "rationale": "Reflects supply chain & input cost shifts."
    },
    "Pharmaceuticals": {
        "ratios": "P/E, EV/EBITDA",
        "rationale": "R&D-led earnings & global exposure."
    },
    "Hospitals": {
        "ratios": "EV/EBITDA, EV/Bed",
        "rationale": "Capital-intensive; EV/Bed standardizes scale."
    },
    "Diagnostics": {
        "ratios": "EV/Sales, P/E",
        "rationale": "High margin, volume-led business."
    },
    "Cement": {
        "ratios": "EV/tonne, EV/EBITDA",
        "rationale": "Tonne value = capacity & replacement cost."
    },
    "Steel / Metals": {
        "ratios": "EV/EBITDA, EV/tonne",
        "rationale": "Commodity price cyclicality normalized."
    },
    "Real Estate": {
        "ratios": "NAV Discount, P/E",
        "rationale": "NAV captures asset base; P/E for stable cash."
    },
    "REITs / INVITs": {
        "ratios": "Dividend Yield, NAV Premium",
        "rationale": "Yield = cash flow attractiveness."
    },
    "Power Utilities": {
        "ratios": "EV/EBITDA, P/B",
        "rationale": "Regulated returns; project viability."
    },
    "Transmission": {
        "ratios": "EV/Circuit KM, P/B",
        "rationale": "Asset base & annuity income focused."
    },
    "Chemicals": {
        "ratios": "EV/EBITDA, P/E",
        "rationale": "High EBITDA margins; specialty contracts."
    },
    "Agrochem / Fertilizers": {
        "ratios": "EV/EBITDA, P/E",
        "rationale": "Seasonal + subsidy/tax-adjusted earnings."
    },
    "Capital Goods / Infra": {
        "ratios": "EV/EBITDA, OB/Sales",
        "rationale": "Order backlog & execution key."
    },
    "Logistics": {
        "ratios": "EV/EBITDA, EV/Tonne",
        "rationale": "Asset-heavy; ops efficiency matters."
    },
    "Telecom": {
        "ratios": "EV/EBITDA, EV/Subscriber",
        "rationale": "Cash flow + subscriber unit valuation."
    },
    "Retail": {
        "ratios": "EV/Sales, P/E",
        "rationale": "Store-level economics & scalability."
    },
    "QSR": {
        "ratios": "EV/Sales, P/E",
        "rationale": "Expansion pace + same-store sales."
    },
    "Media / OTT": {
        "ratios": "EV/EBITDA, EV/Subscriber",
        "rationale": "Platform monetization drives value."
    },
    "Airlines": {
        "ratios": "EV/EBITDA, EV/ASKM",
        "rationale": "Cyclical + traffic & cost metrics."
    }
}


def get_sector_valuation_guidelines(sector_str: str) -> dict:
    """Find key valuation multiples for a sector from the sector-multiple matrix."""
    if not sector_str:
        return {"ratios": "P/E, EV/EBITDA", "rationale": "Standard valuation metrics."}
        
    sec_std = sector_str.lower().strip()
    
    # Try exact match
    for key, val in SECTOR_VALUATION_MATRIX.items():
        if key.lower().strip() == sec_std:
            return val
            
    # Try partial matching on key names
    for key, val in SECTOR_VALUATION_MATRIX.items():
        parts = [p.strip() for p in re.split(r'[/,-]', key.lower())]
        sec_parts = [p.strip() for p in re.split(r'[/,-]', sec_std)]
        for p in parts:
            if any(p in sp or sp in p for sp in sec_parts if len(sp) > 3 and len(p) > 3):
                return val
                
    return {"ratios": "P/E, EV/EBITDA", "rationale": "Standard valuation metrics."}




