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

def get_calendar_quarter(dt: datetime) -> tuple[int, int]:
    """Return the (quarter, year) of the given datetime object."""
    quarter = (dt.month - 1) // 3 + 1
    return quarter, dt.year

def check_existing_quarter_report(symbol: str, reports_dir: Path, today: datetime) -> tuple[bool, str]:
    """Check if a report for this symbol already exists in the same calendar quarter as today."""
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

def generate_report_via_gemini(api_key: str, r: dict, prompt_template: str, today_str: str, model: str = None) -> str:
    """Invoke the Gemini API in 3 chained stages to generate a comprehensive, non-truncated report."""
    symbol = r["symbol"]
    company = r["company"]
    sector = r["industry"]
    cmp = r.get("close", 0.0)
    mcap = r.get("mcap_cr", 0.0)
    
    # Craft metadata
    metadata = f"""
COMPANY: {company}
NSE TICKER: {symbol}
SECTOR: {sector}
REPORT DATE: {today_str}
CMP: Rs. {cmp:.2f}
MARKET CAP: Rs. {mcap:.1f} Cr
YOUR RATING: BUY
12M TARGET: Rs. {cmp * 1.35:.2f} (derived 12-month target with +35% upside)
"""
    
    # Dual-model routing support
    if not model:
        model = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")
        
    print(f"🤖 [MODEL] Route to: {model}")
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={api_key}"
    headers = {"Content-Type": "application/json"}

    # Define a strict instruction to eradicate space repetition loops in tables
    whitespace_rule = (
        "CRITICAL WHITESPACE RULE: You MUST write all markdown tables in a single, highly compact line per row "
        "(e.g., | Particulars | FY24A |). Absolutely DO NOT pad cells with multiple space characters or insert tabs to align the pipe "
        "characters ('|') vertically. Trailing or leading spaces inside table cells are strictly forbidden as they trigger infinite loops "
        "in the Gemini text generation engine and crash the process. Make every table row compact, with exactly one space on each side of the text."
    )
    
    def call_gemini_with_retry(payload, max_retries=8, initial_delay=12):
        delay = initial_delay
        for attempt in range(1, max_retries + 1):
            try:
                response = requests.post(url, headers=headers, json=payload, timeout=180)
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

    # --- STAGE 1: SECTIONS 1 TO 6 ---
    print(f"🚀 [STAGE 1/3] Compiling fundamental metrics & tables (Sections 1-6) for {symbol}...")
    part1_prompt = (
        f"{prompt_template}\n\n"
        f"CRITICAL ASSIGNMENT DIRECTIONS FOR PART 1:\n"
        f"1. You are tasked with generating PART 1 of the report. This must cover ONLY the following sections in order:\n"
        f"   - HEADER BLOCK (You MUST format the Header Block metrics as exactly two wide horizontal tables stacked vertically, exactly in this markdown format:\n"
        f"     \n"
        f"     | Rating | 12M Target Price | Upside % | CMP (Rs.) | Market Cap (Rs. Cr) | 52W High (Rs.) | 52W Low (Rs.) |\n"
        f"     |---|---|---|---|---|---|---|\n"
        f"     | BUY | Rs. X | +35% | Rs. Y | Z Cr | Rs. A | Rs. B |\n"
        f"     \n"
        f"     | P/E (TTM) | P/B (TTM) | ROCE (%) | ROE (%) | EPS (FY26A) | Book Value (Rs.) | Dividend Yield (%) | Face Value (Rs.) | Promoter (%) | FII (%) | DII (%) |\n"
        f"     |---|---|---|---|---|---|---|---|---|---|---|\n"
        f"     | C | D | E% | F% | Rs. G | Rs. H | I% | Rs. J | K% | L% | M% |\n"
        f"     \n"
        f"     Do NOT use any other layout, do NOT merge them, and do NOT use a 6-column key-value grid.)\n"
        f"   - SECTION 2 — INVESTMENT THESIS (5 bullet points)\n"
        f"   - SECTION 3 — BUSINESS OVERVIEW\n"
        f"   - SECTION 4 — INDUSTRY & COMPETITIVE LANDSCAPE (peer comparison table)\n"
        f"   - SECTION 5 — MANAGEMENT QUALITY & CAPITAL ALLOCATION\n"
        f"   - SECTION 6 — FINANCIAL DEEP-DIVE (Income Statement, Balance Sheet, Cash Flow tables with commentary)\n"
        f"2. STOP IMMEDIATELY after completing SECTION 6. Do NOT write anything for Section 7, 8, 9, 10, 10B, or the Appendix.\n"
        f"3. Apply all style rules. For all financial tables, place clickable Screener.in verification links directly BELOW the table. Keep table cells compact.\n"
        f"4. {whitespace_rule}\n\n"
        f"Apply the above structure and guidelines to produce PART 1 of the equity research report for the following company:\n\n"
        f"{metadata}"
    )
    
    res_json1 = call_gemini_with_retry({
        "contents": [{"parts": [{"text": part1_prompt}]}],
        "generationConfig": {"temperature": 0.7, "maxOutputTokens": 8192}
    })
    if not res_json1:
        raise RuntimeError("Failed to generate Stage 1 report.")
    part1_text = res_json1["candidates"][0]["content"]["parts"][0]["text"].strip()
    # Minimize delay on Paid Tier (1s spacing between stages)
    time.sleep(1)
    
    # --- STAGE 2: SECTIONS 7 TO 10B ---
    print(f"🚀 [STAGE 2/3] Compiling valuations, risks & weekly technical setup (Sections 7-10B) for {symbol}...")
    
    # Extract compact context from Part 1 with robust continuity guards
    lines = part1_text.splitlines()
    header_lines = []
    for line in lines:
        if "SECTION 2" in line or ("### SECTION" in line and "HEADER" not in line):
            break
        header_lines.append(line)
    header_context = "\n".join(header_lines).strip()
    if not header_context:
        header_context = metadata.strip()
        
    table1_match = re.search(r"(\*\*TABLE 1 — Income Statement\*\*.*?(?=\*\*TABLE 2|$))", part1_text, re.DOTALL)
    table1_context = table1_match.group(1).strip() if table1_match else "Income statement data was truncated in part 1. Please generate earnings checklist and valuation based on general sector metrics and stock metadata."
    
    compact_context = f"""
{header_context}

---
Here are the exact financial numbers established in Part 1 for consistency:
{table1_context}
"""
    
    part2_prompt = (
        f"{prompt_template}\n\n"
        f"CRITICAL ASSIGNMENT DIRECTIONS FOR PART 2:\n"
        f"1. You are tasked with generating PART 2 of the report, continuing from the previously generated PART 1.\n"
        f"2. PART 2 must cover ONLY the following sections in order:\n"
        f"   - SECTION 7 — EARNINGS QUALITY CHECKLIST (table rating GREEN/AMBER/RED with comment, overall rating)\n"
        f"   - SECTION 8 — VALUATION (You MUST first run the three scenarios as a clean, highly structured markdown table containing rows for BULL, BASE, and BEAR with columns: Scenario | Revenue Growth % | EBITDA Margin % | Projected EPS (Rs.) | Target Multiple | Target Price (Rs.) | Upside/Downside %. Then provide the detailed workings for Method 1: P/E-based target, Method 2: EV/EBITDA-based target, the Blended Target, FCF yield, and the re-rating potential narrative.)\n"
        f"   - SECTION 9 — KEY RISKS (6-7 risks with matrix/probability-impact, monitoring metric)\n"
        f"   - SECTION 10 — RECOMMENDATION (suggested entry zone, investment horizon, three thesis invalidation triggers, ideal profile)\n"
        f"   - SECTION 10B — TECHNICAL LEVELS & CHART STRUCTURE (weekly support/resistance table, weekly EMAs and VStop structure)\n"
        f"3. START DIRECTLY with the header '### SECTION 7 — EARNINGS QUALITY CHECKLIST'. Do NOT repeat any header, metadata, or content from PART 1.\n"
        f"4. STOP IMMEDIATELY after completing SECTION 10B. Do NOT write anything for the Appendix.\n"
        f"5. Maintain absolute consistency with the numbers, estimates, and ratings established in PART 1.\n"
        f"6. Apply all style rules. Keep table columns compact and do not pad with trailing spaces.\n"
        f"7. {whitespace_rule}\n\n"
        f"Here is the context of PART 1 for consistency:\n"
        f"--- START OF PART 1 CONTEXT ---\n"
        f"{compact_context}\n"
        f"--- END OF PART 1 CONTEXT ---\n\n"
        f"Now, generate PART 2 (starting from ### SECTION 7 and stopping after SECTION 10B) for {company} ({symbol}):"
    )
    
    res_json2 = call_gemini_with_retry({
        "contents": [{"parts": [{"text": part2_prompt}]}],
        "generationConfig": {"temperature": 0.7, "maxOutputTokens": 8192}
    })
    if not res_json2:
        raise RuntimeError("Failed to generate Stage 2 report.")
    part2_text = res_json2["candidates"][0]["content"]["parts"][0]["text"].strip()
    # Minimize delay on Paid Tier (1s spacing between stages)
    time.sleep(1)
    
    # --- STAGE 3: APPENDIX (CONCALL BRIEF) & DISCLAIMER ---
    print(f"🚀 [STAGE 3/3] Compiling quarterly earnings concall appendix & disclaimer for {symbol}...")
    
    part2_lines = part2_text.splitlines()
    valuation_lines = []
    capture = False
    for line in part2_lines:
        if "### SECTION 8 — VALUATION" in line or "SECTION 8" in line:
            capture = True
        if "### SECTION 9" in line or "SECTION 9" in line:
            capture = False
        if capture:
            valuation_lines.append(line)
    valuation_context = "\n".join(valuation_lines).strip()
    if not valuation_context:
        valuation_context = "Valuation metrics were truncated in part 2. Please establish the concall signals and final analyst verdict in consistency with a standard BUY rating."
    
    compact_context_part3 = f"""
{header_context}

---
Here are the exact financial numbers established in Part 1 for consistency:
{table1_context}

---
Here is the Valuation context established in Part 2 for consistency:
{valuation_context}
"""
    
    concall_constraint = (
        "CRITICAL VOLUME CONSTRAINT: Keep the APPENDIX — LATEST CONCALL BRIEF extremely dense, fact-focused, and concise. "
        "The entire Part 3 (including the concall appendix and global disclaimer) MUST be under 800 words total. Summarize each of "
        "the 10 sub-sections in 1-2 punchy sentences. Do not use filler words. This is mandatory to prevent truncation."
    )

    part3_prompt = (
        f"{prompt_template}\n\n"
        f"CRITICAL ASSIGNMENT DIRECTIONS FOR PART 3:\n"
        f"1. You are tasked with generating PART 3 (the APPENDIX and DISCLAIMER) of the report.\n"
        f"2. PART 3 must cover the remaining sections in order:\n"
        f"   - APPENDIX — LATEST CONCALL BRIEF (Call grade, signal summary table, to my boss paragraph, 10 sub-sections as described in prompt.md)\n"
        f"   - GLOBAL STYLE RULES & DISCLAIMER\n"
        f"3. START DIRECTLY with the header '### APPENDIX — LATEST CONCALL BRIEF'. Do NOT repeat any header, metadata, or content from PART 1 or PART 2.\n"
        f"4. Maintain absolute consistency with the numbers, estimates, valuations, and ratings established in PART 1 and PART 2.\n"
        f"5. Apply all style rules. Keep table columns compact.\n"
        f"6. {whitespace_rule}\n"
        f"7. {concall_constraint}\n\n"
        f"Here is the context of PART 1 and PART 2 for consistency:\n"
        f"--- START OF CONTEXT ---\n"
        f"{compact_context_part3}\n"
        f"--- END OF CONTEXT ---\n\n"
        f"Now, generate PART 3 (starting from ### APPENDIX — LATEST CONCALL BRIEF) for {company} ({symbol}):"
    )
    
    res_json3 = call_gemini_with_retry({
        "contents": [{"parts": [{"text": part3_prompt}]}],
        "generationConfig": {"temperature": 0.7, "maxOutputTokens": 8192}
    })
    if not res_json3:
        raise RuntimeError("Failed to generate Stage 3 report.")
    part3_text = res_json3["candidates"][0]["content"]["parts"][0]["text"].strip()
    
    full_report = part1_text + "\n\n" + part2_text + "\n\n" + part3_text
    return full_report


def send_report_email(symbol: str, company: str, report_md: str) -> None:
    """Send the newly generated research report as a beautifully rendered, inline HTML email body directly in Gmail (no attachments)."""
    import smtplib
    import re
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

    html_content = markdown_to_html(report_md)
    
    html_body = f"""
    <html>
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
        symbol = r["symbol"]
        company = r["company"]
        
        print(f"\n🔍 Checking report status for `{symbol}` ({company})...")
        
        # Check calendar quarter report existance to avoid repeated generation within same quarter
        exists, quarter_info = check_existing_quarter_report(symbol, reports_dir, today)
        if exists:
            print(f"⏭️  [SKIPPED] A report for {symbol} has already been compiled in the current calendar quarter: {quarter_info}.")
            print("Avoiding repeated token expenditure as no new quarterly earnings result has been released.")
            continue
            
        print(f"✍️  [COMPILING] No report found for {symbol} in the current calendar quarter.")
        
        # Add a small 2-second delay between consecutive compiles to remain safely within standard API limits
        if reports_compiled > 0:
            import time
            print("⏳ Spacing out API requests (2s delay)...")
            time.sleep(2)
            
        print(f"Requesting Gemini AI to generate full Wheels-style equity research report...")
        reports_compiled += 1
        
        try:
            confl_model = os.environ.get("CONFLUENCE_MODEL", "gemini-2.5-pro")
            report_text = generate_report_via_gemini(api_key, r, prompt_template, today_str, model=confl_model)
            report_file = reports_dir / f"{symbol}_equity_report_{date_suffix}.md"
            
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
            consecutive_failures += 1
            if consecutive_failures >= 3:
                print("\n❌ [CRITICAL] 3 consecutive report generation failures occurred. Terminating pipeline to protect API quota.")
                sys.exit(1)
            
    # Load and process emerging leaders (save file & output to log ONLY, no emails)
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
            print(f"🚀 Found {len(emerging_rows)} emerging leaders. Processing and logging reports...")
            print("="*80)
            
            for r in emerging_rows:
                symbol = r["symbol"]
                company = r["company"]
                
                print(f"\n🔍 Checking report status for Emerging Leader: `{symbol}` ({company})...")
                
                exists, quarter_info = check_existing_quarter_report(symbol, reports_dir, today)
                if exists:
                    print(f"⏭️  [SKIPPED] A report for emerging leader {symbol} exists in current quarter: {quarter_info}.")
                    continue
                    
                print(f"✍️  [COMPILING] Compiling report for emerging leader {symbol}...")
                
                if reports_compiled > 0:
                    import time
                    print("⏳ Spacing out API requests (2s delay)...")
                    time.sleep(2)
                    
                reports_compiled += 1
                
                try:
                    emerg_model = os.environ.get("EMERGING_MODEL", "gemini-2.5-flash")
                    report_text = generate_report_via_gemini(api_key, r, prompt_template, today_str, model=emerg_model)
                    report_file = reports_dir / f"{symbol}_equity_report_{date_suffix}.md"
                    
                    with open(report_file, "w") as f:
                        f.write(report_text)
                        
                    print(f"✅ [SUCCESS] Saved report to file: {report_file}")
                    
                    # Commit and push immediately to preserve progress
                    git_commit_and_push(symbol, report_file)
                    
                    # Reset consecutive failure counter on success
                    consecutive_failures = 0
                    
                    # Output the full report to console/log
                    print(f"\n" + "="*80)
                    print(f"📄 [EMERGING LEADER REPORT LOG] {symbol} ({company})")
                    print(f"="*80)
                    print(report_text)
                    print(f"="*80 + "\n")
                    
                except Exception as e:
                    print(f"❌ [FAILED] Error generating report for emerging leader {symbol}: {e}")
                    consecutive_failures += 1
                    if consecutive_failures >= 3:
                        print("\n❌ [CRITICAL] 3 consecutive report generation failures occurred. Terminating pipeline to protect API quota.")
                        sys.exit(1)
    else:
        print("\nℹ️ No emerging leaders list found at outputs/today_emerging.json. Skipping.")

    print("\n" + "="*80)
    print("🏆 Deep Equity Research Report compilation process complete!")
    print("="*80)

if __name__ == "__main__":
    main()
