import os
import subprocess
from pathlib import Path

report_path = Path("outputs/reports/SUPRIYA_equity_report_2026-05-31.md")
if report_path.exists():
    os.remove(report_path)
    print("🗑️ Deleted conflicted/truncated SUPRIYA report file.")

# Run the generator script
print("🚀 Triggering fresh generation under Paid Tier...")
subprocess.run(["./venv/bin/python3", "-u", "generate_equity_reports.py"])
