
import json
import pandas as pd

json_path = "/Users/monitranjan/.gemini/antigravity/scratch/scan_matched_data.json"
csv_output_path = "/Users/monitranjan/Downloads/multibagger-scanner/scan_matched_stocks_2026-05-26.csv"
markdown_report_path = "/Users/monitranjan/Downloads/multibagger-scanner/scan_match_report.md"

with open(json_path, "r") as f:
    data = json.load(f)

companies = data.get("companies", [])
print(f"Loaded {len(companies)} companies.")

rows = []
for c in companies:
    symbol = c.get("companyId", "")
    name = c.get("Name", "")
    industry = c.get("Industry", "")
    mcap = c.get("Market Capitalization", 0.0)
    close = c.get("Close Price", 0.0)
    ret1d = c.get("Returns 1D", 0.0)
    ret1w = c.get("Returns 1W", 0.0)
    scans = c.get("Scans", 0)
    
    # Compile scan names
    saved_scans = [s.get("scanName", "") for s in c.get("savedScans", [])]
    popular_scans = [p.get("scanName", "") for p in c.get("popularScans", [])]
    all_scan_names = saved_scans + popular_scans
    scan_list_str = ", ".join(all_scan_names)
    
    rows.append({
        "Symbol": symbol,
        "Name": name,
        "Industry": industry,
        "Market Cap (Cr)": mcap,
        "Close Price (₹)": close,
        "Returns 1D (%)": ret1d,
        "Returns 1W (%)": ret1w,
        "Scan Matches Count": scans,
        "Matches": scan_list_str
    })

df = pd.DataFrame(rows)
df = df.sort_values(by="Scan Matches Count", ascending=False).reset_index(drop=True)

# Save to CSV
df.to_csv(csv_output_path, index=False)
print(f"Saved CSV to {csv_output_path}")

# Generate beautiful Markdown report
md_lines = [
    "# 📈 Live StockScans Scan-Matched Report",
    f"Generated on 26 May 2026 (Live data fetched via authenticated session cookie).",
    f"Total common stocks appearing in multiple scans: **{len(df)}**.",
    "",
    "## 🏆 Top 35 Scan-Matched Stocks (Highest Confluence)",
    "These stocks appear most frequently across your custom and popular scans, indicating strong bullish confirmation across multiple parameters.",
    "",
    "| Rank | Symbol | Company Name | Industry | Close (₹) | 1D Ret (%) | 1W Ret (%) | Mcap (Cr) | Scan Count |",
    "|---|---|---|---|---|---|---|---|---|"
]

for idx, row in df.head(35).iterrows():
    md_lines.append(
        f"| {idx + 1} | `{row['Symbol']}` | {row['Name']} | {row['Industry']} | ₹{row['Close Price (₹)']:,.2f} | {row['Returns 1D (%)']}% | {row['Returns 1W (%)']}% | ₹{row['Market Cap (Cr)']:,.1f} | **{row['Scan Matches Count']}** |"
    )

md_lines += [
    "",
    "## 🔍 Detailed Scan Confluences (Top 10 Stocks)",
    "Here are the specific scans that each of the top 10 stocks matched on:",
    ""
]

for idx, row in df.head(10).iterrows():
    md_lines.append(f"### {idx + 1}. {row['Name']} ({row['Symbol']}) — **{row['Scan Matches Count']} Scans**")
    md_lines.append(f"- **Industry**: {row['Industry']}")
    md_lines.append(f"- **Close**: ₹{row['Close Price (₹)']:,.2f} ({row['Returns 1D (%)']}% Today | {row['Returns 1W (%)']}% 1-Week)")
    md_lines.append(f"- **Matched Scans**: {row['Matches']}")
    md_lines.append("")

with open(markdown_report_path, "w") as f_md:
    f_md.write("\n".join(md_lines))

print(f"Saved Markdown report to {markdown_report_path}")
