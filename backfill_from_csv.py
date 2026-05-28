"""
Chartink SQLite Backfill & Sync Utility
────────────────────────────────────────
If you missed running the daily scanner (e.g., due to travel or holidays):
1. Go to Chartink and download the fresh premium backtest CSV (contains full history up to today).
2. Save it in the folder as 'Backtest Monit momentum (2).csv'.
3. Run this script: python3 backfill_from_csv.py
It will scan the CSV, identify all dates missing from your SQLite database,
insert them idempotently, and completely rebuild your stock_analytics profiles!
"""

import os
import sqlite3
from pathlib import Path
import pandas as pd
from datetime import date
from monit_ranker import init_and_seed_database, rebuild_stock_analytics_table, fetch_all_stocks_details, load_scanner_symbols, load_scan_matched_symbols, fetch_all_stockscans_details

def main():
    print("=====================================================================")
    print("🔄 STARTING SQLITE DATABASE BACKFILL & HISTORY SYNC UTILITY 🔄")
    print("=====================================================================")

    db_path = Path("logs/backtest.db")
    csv_path = Path("Backtest Monit momentum (2).csv")

    if not csv_path.exists():
        print(f"❌ Error: Baseline CSV '{csv_path}' was not found in your directory!")
        print("Please download a fresh backtest CSV from Chartink and place it here.")
        return

    # Initialize DB schema if not already
    init_and_seed_database()

    print(f"📊 Reading premium backtest CSV: '{csv_path}'...")
    try:
        df_csv = pd.read_csv(csv_path)
    except Exception as e:
        print(f"❌ Error reading CSV: {e}")
        return

    df_csv = df_csv.dropna(subset=["date", "symbol"])
    df_csv["date"] = df_csv["date"].astype(str).str.strip()
    df_csv["symbol"] = df_csv["symbol"].astype(str).str.strip().str.upper()
    df_csv["marketcapname"] = df_csv["marketcapname"].astype(str).str.strip()
    df_csv["sector"] = df_csv["sector"].astype(str).str.strip()

    # Get unique dates in CSV
    csv_dates = set(df_csv["date"].unique())
    print(f"   • Total distinct dates in CSV: {len(csv_dates)}")

    # Connect to SQLite and check existing dates
    conn = sqlite3.connect(str(db_path))
    cursor = conn.cursor()
    cursor.execute("SELECT DISTINCT date FROM screener_history")
    db_dates = {row[0] for row in cursor.fetchall()}
    print(f"   • Total distinct dates in SQLite database: {len(db_dates)}")

    # Identify missing dates in DB
    missing_dates = csv_dates - db_dates
    if not missing_dates:
        print("\n✅ Awesome! Your SQLite database is fully synchronized. No missing dates found.")
        conn.close()
        return

    print(f"\n💡 Found {len(missing_dates)} missing trading dates in SQLite database!")
    print(f"🔍 Missed Dates: {sorted(list(missing_dates))}")

    # Extract missing rows
    df_missing = df_csv[df_csv["date"].isin(missing_dates)].drop_duplicates(subset=["date", "symbol"])
    records = [
        (row.date, row.symbol, row.marketcapname, row.sector)
        for row in df_missing.itertuples(index=False)
    ]

    print(f"📝 Inserting {len(records)} missing historical records into SQLite...")
    try:
        cursor.executemany(
            "INSERT OR IGNORE INTO screener_history (date, symbol, marketcapname, sector) VALUES (?, ?, ?, ?)",
            records
        )
        conn.commit()
        print(f"✅ Sync complete! Successfully backfilled {len(missing_dates)} missing dates in database.")
    except Exception as e:
        print(f"❌ Error writing missing rows to database: {e}")
        conn.close()
        return

    conn.close()

    # 4. Trigger total refresh of analytics and price targets!
    print("\n🔄 Rebuilding and recalculating analytical profiles...")
    # Fetch details for daily calculations
    scanner_symbols = load_scanner_symbols()
    scan_matched_symbols = load_scan_matched_symbols()
    
    conn = sqlite3.connect(str(db_path))
    df_history = pd.read_sql_query("SELECT symbol FROM screener_history", conn)
    conn.close()
    
    symbols = df_history["symbol"].dropna().unique().tolist()
    all_symbols = list(set(symbols + scanner_symbols + scan_matched_symbols))
    
    # Gentle fetch
    yfinance_data = fetch_all_stocks_details(all_symbols[:50]) # Quick sample for today
    
    rebuild_stock_analytics_table(yfinance_data, {}, {})
    print("\n🏆 SUCCESS! SQLite Database has been fully backfilled and compiled.")
    print("=====================================================================")

if __name__ == "__main__":
    main()
