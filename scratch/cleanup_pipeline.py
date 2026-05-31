import os
import subprocess
from pathlib import Path

# Files to remove
bad_reports = [
    "outputs/reports/HFCL_equity_report_2026-05-30.md",
    "outputs/reports/AVALON_equity_report_2026-05-31.md",
    "outputs/reports/SUPRIYA_equity_report_2026-05-31.md",
    "outputs/reports/TEST_email_rendered.html"
]

unwanted_pdfs = [
    "Gmail - 🏆 New Monit Institutional Research Report — SUPRIYA (Supriya Lifescience Ltd).pdf"
]

print("🧹 Starting Repository Cleanup...")

# 1. Remove bad reports
for r in bad_reports:
    p = Path(r)
    if p.exists():
        os.remove(p)
        print(f"🗑️ Removed bad report file: {p.name}")

# 2. Remove unwanted PDFs
for pdf in unwanted_pdfs:
    p = Path(pdf)
    if p.exists():
        os.remove(p)
        print(f"🗑️ Removed unwanted PDF: {p.name}")

# 3. Synchronize Git deletions
print("📦 Syncing repository changes to GitHub...")
try:
    # Run git add -A to track deletions
    subprocess.run(["git", "add", "-A"], check=True)
    
    # Check if there is anything to commit
    diff_res = subprocess.run(["git", "diff", "--quiet", "--staged"], check=False)
    if diff_res.returncode != 0:
        subprocess.run(["git", "commit", "-m", "chore: cleanup truncated reports and untracked assets from repo"], check=True)
        subprocess.run(["git", "pull", "--rebase", "--autostash", "origin", "main"], check=True)
        subprocess.run(["git", "push", "origin", "main"], check=True)
        print("🚀 Successfully pushed cleanup to GitHub!")
    else:
        print("ℹ️ Repo is already clean.")
except Exception as e:
    print(f"⚠️ Git error: {e}")
