import os
from pathlib import Path

reports_dir = Path("outputs/reports")
if not reports_dir.exists():
    print("No reports folder found.")
    exit(0)

print("🔍 Scanning reports directory...")
for filepath in reports_dir.glob("*.md"):
    content = filepath.read_text(errors="ignore")
    has_disclaimer = "GLOBAL STYLE RULES & DISCLAIMER" in content
    has_conflicts = "<<<<<<<" in content or "=======" in content or ">>>>>>>" in content
    has_whitespace_loop = "   " * 100 in content
    
    status = []
    if not has_disclaimer:
        status.append("❌ TRUNCATED (No Disclaimer)")
    if has_conflicts:
        status.append("⚠️ GIT CONFLICTS PRESENT")
    if has_whitespace_loop:
        status.append("🚨 WHITESPACE LOOP DETECTED (Bloated File)")
        
    if not status:
        print(f"✅ {filepath.name} ({len(content)/1024:.1f} KB) - Pristine!")
    else:
        print(f"{', '.join(status)} - {filepath.name} ({len(content)/1024:.1f} KB)")
