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

def generate_report_via_gemini(api_key: str, r: dict, prompt_template: str, today_str: str, model: str = None) -> str:
    """Invoke the Gemini API in three distinct stages to guarantee complete, non-truncated reports."""
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
12M TARGET: (Please calculate dynamically based on peer multiples, financial data, and your valuation modeling)
"""
    
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
        f"Now, generate PART 2 (starting from ### SECTION 6 — FINANCIAL DEEP-DIVE) for {company} ({symbol}):"
    )

    stage2_headers = ["SECTION 6", "SECTION 7"]
    part2_text = call_stage_with_fallback(2, stage2_prompt, stage2_headers, model)
    
    # Prepare compact context for Stage 3 (Header + Table 1 from part2_text)
    table1_match = re.search(r"(####?\s*TABLE 1\b.*?(?=####?\s*TABLE 2\b|###\s*SECTION|$))", part2_text, re.DOTALL | re.IGNORECASE)
    table1_context = table1_match.group(1).strip() if table1_match else ""
    
    compact_context_part3 = f"{header_context}\n\n{table1_context}".strip()
    
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


def send_emerging_digest_email(compiled_reports: list[dict]) -> None:
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
    
    # 1. Build the summary table rows
    table_rows_html = []
    
    for idx, item in enumerate(compiled_reports):
        r = item["r"]
        symbol = item["symbol"]
        company = item["company"]
        sector = r.get("industry", "N/A")
        cmp = r.get("close", 0.0)
        mcap = r.get("mcap_cr", 0.0)
        
        bg_color = "#f8f9fa" if idx % 2 == 1 else "#ffffff"
        report_md = item["report_md"]
        target_price, upside_pct = extract_target_and_upside(report_md, cmp)
        
        table_rows_html.append(f"""
        <tr style="background-color: {bg_color};">
          <td style="border: 1px solid #e2e8f0; padding: 10px; font-weight: bold; color: #1b365d; font-family: sans-serif;">
            <a href="#report-{symbol}" style="color: #1b365d; text-decoration: none; border-bottom: 1px dashed #1b365d;">{symbol}</a>
          </td>
          <td style="border: 1px solid #e2e8f0; padding: 10px; color: #2d3748; font-family: sans-serif;">{company}</td>
          <td style="border: 1px solid #e2e8f0; padding: 10px; color: #4a5568; font-family: sans-serif;">{sector}</td>
          <td style="border: 1px solid #e2e8f0; padding: 10px; text-align: right; color: #2d3748; font-family: sans-serif;">₹{cmp:,.2f}</td>
          <td style="border: 1px solid #e2e8f0; padding: 10px; text-align: right; font-weight: bold; color: #2e7d32; font-family: sans-serif;">₹{target_price:,.2f} (+{upside_pct:.1f}%)</td>
          <td style="border: 1px solid #e2e8f0; padding: 10px; text-align: right; color: #4a5568; font-family: sans-serif;">₹{mcap:,.1f} Cr</td>
        </tr>
        """)
        
    summary_table_html = f"""
    <div style="overflow-x: auto; margin: 20px 0;">
      <table style="border-collapse: collapse; width: 100%; border: 1px solid #e2e8f0;">
        <thead>
          <tr style="background-color: #1b365d; color: white;">
            <th style="border: 1px solid #e2e8f0; padding: 12px 10px; text-align: left; font-family: sans-serif; font-size: 14px;">Ticker</th>
            <th style="border: 1px solid #e2e8f0; padding: 12px 10px; text-align: left; font-family: sans-serif; font-size: 14px;">Company Name</th>
            <th style="border: 1px solid #e2e8f0; padding: 12px 10px; text-align: left; font-family: sans-serif; font-size: 14px;">Industry/Sector</th>
            <th style="border: 1px solid #e2e8f0; padding: 12px 10px; text-align: right; font-family: sans-serif; font-size: 14px;">CMP</th>
            <th style="border: 1px solid #e2e8f0; padding: 12px 10px; text-align: right; font-family: sans-serif; font-size: 14px;">12M Target (Upside)</th>
            <th style="border: 1px solid #e2e8f0; padding: 12px 10px; text-align: right; font-family: sans-serif; font-size: 14px;">MCap (Cr)</th>
          </tr>
        </thead>
        <tbody>
          {"".join(table_rows_html)}
        </tbody>
      </table>
    </div>
    """
    
    # 2. Build both flat reports (with jump-links) and collapsible details sections
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
        
        report_html = markdown_to_html(report_md)
        
        # A. Flat report block with jump-links (for mobile email body)
        reports_body_html.append(f"""
        <div id="report-{symbol}">
          <a name="report-{symbol}"></a>
          <h3 style="color: #2e7d32; border-bottom: 2px solid #2e7d32; padding-bottom: 6px; margin-top: 50px; font-family: sans-serif; text-transform: uppercase;">📄 {symbol} — {company} Research Report</h3>
          <div style="padding: 15px 0; background-color: white;">
            {report_html}
          </div>
          <div style="text-align: right; margin-top: 10px; margin-bottom: 20px;">
            <a href="#summary-dashboard" style="color: #2e7d32; font-weight: bold; text-decoration: none; font-size: 13.5px; font-family: sans-serif; border: 1px solid #2e7d32; padding: 6px 12px; border-radius: 4px; background-color: #f0fdf4;">[Back to Dashboard Table ↑]</a>
          </div>
          <hr style="border: 0; border-top: 2px dashed #cbd5e0; margin: 40px 0;">
        </div>
        """)
        
        # B. Collapsible report block (for attached HTML file)
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
            {report_html}
            <div style="text-align: right; margin-top: 20px; border-top: 1px solid #edf2f7; padding-top: 15px;">
              <a href="#summary-dashboard" style="color: #2e7d32; font-weight: bold; text-decoration: none; font-size: 13px; font-family: sans-serif; border: 1px solid #2e7d32; padding: 5px 10px; border-radius: 4px; background-color: #f0fdf4; margin-right: 10px;">[Back to Dashboard Table ↑]</a>
              <button onclick="document.getElementById('report-{symbol}').open = false; window.location.hash = '#summary-dashboard';" style="color: #e53e3e; font-weight: bold; text-decoration: none; font-size: 13px; font-family: sans-serif; border: 1px solid #e53e3e; padding: 5px 10px; border-radius: 4px; background-color: #fff5f5; cursor: pointer; border-style: solid;">[Collapse Report ✕]</button>
            </div>
          </div>
        </details>
        """)
        
    reports_body_joined = "\n".join(reports_body_html)
        
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
          {summary_table_html}
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
              💡 Mobile Quick Navigation Active:
            </p>
            <p style="margin: 3px 0 0 0; font-size: 13px; color: #2e7d32; line-height: 1.5;">
              Tap any stock's <strong>Ticker Symbol</strong> in the dashboard table below to jump directly down to its report. Tap <code>[Back to Dashboard Table ↑]</code> at the end of any report to scroll back up instantly.
            </p>
            <p style="margin: 8px 0 0 0; font-size: 13px; color: #2e7d32; line-height: 1.5;">
              📎 <strong>Interactive Collapsible Attachment Included:</strong> Open the attached HTML file (<code>Emerging_Leaders_Digest_{today_str.replace(' ', '_')}.html</code>) on any laptop/browser to view reports in a premium collapsible layout.
            </p>
          </div>
          
          <a name="summary-dashboard" id="summary-dashboard"></a>
          <h3 style="color: #2e7d32; border-bottom: 2px solid #e2e8f0; padding-bottom: 6px; margin-top: 25px; font-family: sans-serif;">📊 Executive Summary Dashboard</h3>
          {summary_table_html}
          
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
            plain_text += f"* {item['symbol']} ({item['company']})\n"
        body_parts.attach(MIMEText(plain_text, "plain"))
        body_parts.attach(MIMEText(html_body, "html"))
        msg.attach(body_parts)
        
        # Attach the interactive collapsible HTML dashboard file
        attachment = MIMEApplication(interactive_html.encode("utf-8"), _subtype="html")
        attachment.add_header("Content-Disposition", "attachment", filename=f"Emerging_Leaders_Digest_{today_str.replace(' ', '_')}.html")
        msg.attach(attachment)
        print("📎 Attached consolidated interactive HTML dashboard to digest email.")
        
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
            
            compiled_emerging_reports = []
            
            for r in emerging_rows:
                symbol = r["symbol"]
                company = r["company"]
                
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
                                
                            compiled_emerging_reports.append({
                                "symbol": symbol,
                                "company": company,
                                "report_md": report_text,
                                "r": r
                            })
                            print(f"📋 Loaded existing report for {symbol} into today's digest.")
                        except Exception as read_err:
                            print(f"⚠️ Failed to read existing report for {symbol}: {read_err}")
                    continue
                    
                print(f"✍️  [COMPILING] Compiling report for emerging leader {symbol}...")
                
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
                        "r": r
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
