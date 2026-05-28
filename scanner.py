"""
TIMELOSS 200EMA ATH Multibagger Scanner
────────────────────────────────────────
Fetches stocks from Chartink screener, applies your Pine Script logic in Python,
ranks them by signal strength, and sends alerts via Email + Telegram + WhatsApp + CSV.

Capital: ₹10 lakhs | Max positions: 20-25 | Exchange: NSE + BSE
"""

import os
def load_dotenv():
    """Load variables from .env file into os.environ if it exists."""
    from pathlib import Path
    env_path = Path(".env")
    if env_path.exists():
        with open(env_path, "r") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    key, val = line.split("=", 1)
                    os.environ[key.strip()] = val.strip().strip('"').strip("'")

load_dotenv()
import requests
import pandas as pd
import numpy as np
from bs4 import BeautifulSoup
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, date
import smtplib
import json
import time
import logging
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
import yfinance as yf

# ─── LOGGING ──────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)s  %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("logs/scanner.log", mode="a"),
    ],
)
log = logging.getLogger(__name__)

# ─── CONFIG (set via GitHub Secrets — see README) ─────────────────────────────
GMAIL_USER      = os.environ.get("GMAIL_USER", "")
GMAIL_APP_PASS  = os.environ.get("GMAIL_APP_PASS", "")
ALERT_EMAIL     = os.environ.get("ALERT_EMAIL", GMAIL_USER)

TELEGRAM_TOKEN  = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT   = os.environ.get("TELEGRAM_CHAT_ID", "")

TWILIO_SID      = os.environ.get("TWILIO_SID", "")
TWILIO_TOKEN    = os.environ.get("TWILIO_TOKEN", "")
TWILIO_FROM     = os.environ.get("TWILIO_WHATSAPP_FROM", "whatsapp:+14155238886")
TWILIO_TO       = os.environ.get("TWILIO_WHATSAPP_TO", "")   # e.g. whatsapp:+919876543210

# ─── STRATEGY PARAMS (mirrors your Pine Script) ───────────────────────────────
RSI_MIN         = 60          # RSI minimum threshold
ATH_THRESHOLD1  = 30          # Max % below ATH for Entry 1 & 2
ATH_THRESHOLD2  = 15          # Max % below ATH for Entry 3 (already running)
TIME_STOP_DAYS  = 90
MIN_RETURN_PCT  = 10

# ─── PORTFOLIO CONFIG ─────────────────────────────────────────────────────────
TOTAL_CAPITAL   = 1_000_000   # ₹10 lakhs
MAX_POSITIONS   = 22          # ~20–25 as you said

# Position sizing by entry type (% of capital)
POSITION_SIZE = {
    "EMA Crossover": 0.07,   # 7% per stock → up to ~14 crossover positions
    "52W Breakout":  0.05,   # 5% per stock → comfortable for 20 positions
    "ATH Momentum":  0.04,   # 4% per stock → slightly smaller bet
}

# ─── CHARTINK SCREENER ────────────────────────────────────────────────────────
SCAN_CLAUSE = (
    "( {cash} ( "
    " daily close >  daily ema (  daily close , 200 ) "
    "and  daily close >  (  daily max ( 252 ,  daily high ) *  0.80 ) "
    "and  daily rsi(14) >  55 "
    "and  market cap >=  500 "
    ") )"
)

def fetch_chartink_stocks() -> pd.DataFrame:
    """Fetch stock list from Chartink screener via their POST API."""
    log.info("Fetching stocks from Chartink...")
    session = requests.Session()
    session.headers.update({
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        )
    })

    try:
        r = session.get("https://chartink.com/screener/", timeout=30)
        soup = BeautifulSoup(r.content, "html.parser")
        csrf = soup.find("meta", {"name": "csrf-token"})["content"]

        payload = {"scan_clause": SCAN_CLAUSE}
        headers = {
            "Referer":      "https://chartink.com/screener/",
            "x-csrf-token": csrf,
        }
        resp = session.post(
            "https://chartink.com/screener/process",
            headers=headers,
            data=payload,
            timeout=60,
        )
        resp.raise_for_status()
        data = resp.json()
        if data.get("scan_error"):
            log.error(f"Chartink scan error: {data['scan_error']}")
            return pd.DataFrame()
        df = pd.DataFrame(data.get("data", []))
        log.info(f"Chartink returned {len(df)} stocks")

        if len(df) == 0:
            # Quick sanity probe to distinguish strict filter vs API/session issue.
            sanity_payload = {"scan_clause": "( {cash} ( market cap >= 500 ) )"}
            sanity_resp = session.post(
                "https://chartink.com/screener/process",
                headers=headers,
                data=sanity_payload,
                timeout=60,
            )
            sanity_resp.raise_for_status()
            sanity_data = sanity_resp.json()
            sanity_df = pd.DataFrame(sanity_data.get("data", []))

            if len(sanity_df) > 0:
                log.warning(
                    "Chartink connectivity is OK, but strategy filter returned 0 today."
                )
            else:
                log.warning(
                    "Chartink sanity query also returned 0. Likely API/session issue "
                    "or temporary blocking/rate-limiting."
                )
            log.info(f"Sanity query returned {len(sanity_df)} stocks")

        return df

    except Exception as e:
        log.error(f"Chartink fetch failed: {e}")
        return pd.DataFrame()


# ─── INDICATOR CALCULATIONS ───────────────────────────────────────────────────
def calc_ema(series: pd.Series, span: int) -> pd.Series:
    return series.ewm(span=span, adjust=False).mean()

def calc_rsi(series: pd.Series, period: int = 14) -> pd.Series:
    delta = series.diff()
    gain  = delta.clip(lower=0).rolling(period).mean()
    loss  = (-delta.clip(upper=0)).rolling(period).mean()
    rs    = gain / loss.replace(0, np.nan)
    return 100 - (100 / (1 + rs))


def normalize_yfinance_df(df: pd.DataFrame, ticker: str) -> pd.DataFrame:
    """Normalize yfinance's current single-ticker MultiIndex output."""
    if isinstance(df.columns, pd.MultiIndex):
        if ticker in df.columns.get_level_values(-1):
            df = df.xs(ticker, axis=1, level=-1)
        else:
            df.columns = [c[0] for c in df.columns]
    return df.loc[:, ~df.columns.duplicated()]


# ─── STOCK SCORER ─────────────────────────────────────────────────────────────
def score_stock(nsecode: str):
    """
    Download daily OHLC for one stock and apply Pine Script entry logic.
    Returns a dict with entry type, score, and key metrics. None if data missing.
    """
    # Try NSE first, then BSE
    for suffix in [".NS", ".BO"]:
        ticker = nsecode + suffix
        try:
            df = yf.download(
                ticker,
                period="2y",
                interval="1d",
                progress=False,
                auto_adjust=True,
                threads=False,
            )
            if df is not None and len(df) >= 60:
                df = normalize_yfinance_df(df, ticker)
                break
        except Exception:
            df = pd.DataFrame()
    else:
        return None

    if df.empty or len(df) < 60:
        return None

    close = df["Close"].squeeze()
    high  = df["High"].squeeze()

    ema200      = calc_ema(close, 200)
    rsi14       = calc_rsi(close, 14)
    ath         = high.cummax()
    pct_ath     = ((ath - close) / ath * 100)

    # Rolling highs for 52-week high check
    high52      = close.rolling(252, min_periods=50).max()

    # Latest values
    c   = float(close.iloc[-1])
    e   = float(ema200.iloc[-1])
    r   = float(rsi14.iloc[-1])
    p   = float(pct_ath.iloc[-1])
    c_p = float(close.iloc[-2]) if len(close) > 1 else c
    e_p = float(ema200.iloc[-2]) if len(ema200) > 1 else e

    above_ema    = c > e
    weekly_mom   = c > float(close.iloc[-6])  if len(close) >= 6  else False
    monthly_mom  = c > float(close.iloc[-22]) if len(close) >= 22 else False
    cross_today  = (c_p < e_p) and (c > e)   # yesterday below, today above
    new_52w_high = c >= float(high52.iloc[-2]) if len(high52) > 1 else False

    # ── Entry classification (exact Pine Script logic) ──
    if cross_today and p <= ATH_THRESHOLD1 and r > RSI_MIN and monthly_mom:
        entry = "EMA Crossover"
        score = 100 + (RSI_MIN - p)  # bonus for closeness to ATH
    elif above_ema and new_52w_high and p <= ATH_THRESHOLD1 and r > RSI_MIN and monthly_mom:
        entry = "52W Breakout"
        score = 80 + max(0, 60 - p)
    elif above_ema and p <= ATH_THRESHOLD2 and r > RSI_MIN and weekly_mom and monthly_mom:
        entry = "ATH Momentum"
        score = 60 + max(0, 15 - p)
    else:
        return None   # No signal — skip entirely

    # ── Position sizing ──
    alloc_pct   = POSITION_SIZE[entry]
    alloc_inr   = round(TOTAL_CAPITAL * alloc_pct)
    qty         = max(1, int(alloc_inr / c))

    return {
        "ticker":      nsecode,
        "close":       round(c, 2),
        "ema200":      round(e, 2),
        "rsi":         round(r, 1),
        "pct_ath":     round(p, 1),
        "above_ema":   above_ema,
        "weekly_mom":  weekly_mom,
        "monthly_mom": monthly_mom,
        "entry":       entry,
        "score":       round(score, 1),
        "alloc_pct":   f"{int(alloc_pct*100)}%",
        "alloc_inr":   alloc_inr,
        "qty":         qty,
        "date":        str(date.today()),
    }


# ─── PARALLEL RUNNER ──────────────────────────────────────────────────────────
def run_scanner(stocks_df: pd.DataFrame) -> pd.DataFrame:
    tickers = stocks_df["nsecode"].dropna().unique().tolist()
    log.info(f"Scoring {len(tickers)} stocks in parallel (20 threads)...")

    results = []
    with ThreadPoolExecutor(max_workers=20) as ex:
        futures = {ex.submit(score_stock, t): t for t in tickers}
        for i, f in enumerate(as_completed(futures), 1):
            t = futures[f]
            try:
                res = f.result()
                if res:
                    results.append(res)
                    log.info(f"  [{i}/{len(tickers)}] {t:15s} → {res['entry']:8s}  score={res['score']}")
                else:
                    log.info(f"  [{i}/{len(tickers)}] {t:15s} → no signal")
            except Exception as e:
                log.warning(f"  [{i}/{len(tickers)}] {t} error: {e}")
            time.sleep(0.05)   # gentle rate limit

    if not results:
        return pd.DataFrame()

    df = pd.DataFrame(results)
    df = df.sort_values("score", ascending=False).head(MAX_POSITIONS).reset_index(drop=True)
    df.index += 1
    return df


# ─── SAVE CSV ─────────────────────────────────────────────────────────────────
def save_csv(df: pd.DataFrame):
    fname = f"logs/signals_{date.today()}.csv"
    df.to_csv(fname)
    log.info(f"Saved → {fname}")
    return fname


# ─── FORMAT REPORT ────────────────────────────────────────────────────────────
ENTRY_EMOJI = {"EMA Crossover": "🟢", "52W Breakout": "🟡", "ATH Momentum": "🔵"}

def format_report(df: pd.DataFrame) -> tuple[str, str]:
    """Returns (plain_text, html) report."""
    today   = date.today().strftime("%d %b %Y")
    n_cross = len(df[df["entry"] == "EMA Crossover"])
    n_new   = len(df[df["entry"] == "52W Breakout"])
    n_run   = len(df[df["entry"] == "ATH Momentum"])
    total_deployed = df["alloc_inr"].sum()

    # Plain text
    lines = [
        f"📊 MULTIBAGGER SCANNER — {today}",
        f"🟢 EMA Crossover: {n_cross}  🟡 52W Breakout: {n_new}  🔵 ATH Momentum: {n_run}",
        f"Capital deployed: ₹{total_deployed:,.0f} / ₹{TOTAL_CAPITAL:,.0f}",
        "─" * 60,
    ]
    for _, row in df.iterrows():
        e = ENTRY_EMOJI.get(row["entry"], "⚪")
        lines.append(
            f"{e} {row['ticker']:12s} | ₹{row['close']:>8.2f} | "
            f"RSI {row['rsi']:4.1f} | {row['pct_ath']:4.1f}% from ATH | "
            f"{row['entry']:13s} | Alloc ₹{row['alloc_inr']:>7,} ({row['qty']} qty)"
        )
    lines += [
        "─" * 60,
        "🟢 EMA Crossover = EMA just crossed (BEST entry)",
        "🟡 52W Breakout = 52-week breakout above EMA",
        "🔵 ATH Momentum = Strong trend near ATH",
        "",
        "⚠️  This is a scan alert, not financial advice.",
        "    Always check charts on TradingView before buying.",
    ]
    plain = "\n".join(lines)

    # HTML for email
    rows_html = ""
    for _, row in df.iterrows():
        e = ENTRY_EMOJI.get(row["entry"], "⚪")
        bg = {"EMA Crossover": "#e6ffe6", "52W Breakout": "#fffbe6", "ATH Momentum": "#e6f0ff"}.get(row["entry"], "#fff")
        rows_html += f"""
        <tr style="background:{bg}">
          <td style="padding:8px;font-weight:bold">{e} {row['ticker']}</td>
          <td style="padding:8px;text-align:right">₹{row['close']:,.2f}</td>
          <td style="padding:8px;text-align:right">{row['ema200']:,.2f}</td>
          <td style="padding:8px;text-align:right">{row['rsi']}</td>
          <td style="padding:8px;text-align:right">{row['pct_ath']}%</td>
          <td style="padding:8px;font-weight:bold;color:{'#1a7a1a' if row['entry']=='EMA Crossover' else '#7a6a00' if row['entry']=='52W Breakout' else '#003e7a'}">{row['entry']}</td>
          <td style="padding:8px;text-align:right">₹{row['alloc_inr']:,}</td>
          <td style="padding:8px;text-align:right">{row['qty']}</td>
        </tr>"""

    html = f"""
    <html><body style="font-family:sans-serif;max-width:900px;margin:auto">
    <h2 style="color:#1a1a2e">📊 Multibagger Scanner — {today}</h2>
    <p>
      <b style="color:#1a7a1a">🟢 EMA Crossover: {n_cross}</b> &nbsp;
      <b style="color:#7a6a00">🟡 52W Breakout: {n_new}</b> &nbsp;
      <b style="color:#003e7a">🔵 ATH Momentum: {n_run}</b> &nbsp;&nbsp;|&nbsp;&nbsp;
      Capital deployed: <b>₹{total_deployed:,.0f}</b> of ₹{TOTAL_CAPITAL:,.0f}
    </p>
    <table border="1" cellspacing="0" style="border-collapse:collapse;width:100%">
      <thead style="background:#1a1a2e;color:white">
        <tr>
          <th style="padding:8px">Stock</th>
          <th style="padding:8px">Close</th>
          <th style="padding:8px">200 EMA</th>
          <th style="padding:8px">RSI</th>
          <th style="padding:8px">% from ATH</th>
          <th style="padding:8px">Entry</th>
          <th style="padding:8px">Alloc (₹)</th>
          <th style="padding:8px">Qty</th>
        </tr>
      </thead>
      <tbody>{rows_html}</tbody>
    </table>
    <br>
    <p style="color:#555;font-size:12px">
      🟢 <b>EMA Crossover</b> = EMA just crossed above today (best entry, deploy 7% per stock)<br>
      🟡 <b>52W Breakout</b> = 52-week breakout above 200 EMA (deploy 5%)<br>
      🔵 <b>ATH Momentum</b> = Already in strong uptrend near ATH (deploy 4%)<br><br>
      ⚠️ This is an automated scan alert, not financial advice.
      Always verify on TradingView before buying.
    </p>
    </body></html>
    """
    return plain, html


# ─── ALERTS ───────────────────────────────────────────────────────────────────
def send_email(plain: str, html: str):
    if not GMAIL_USER or not GMAIL_APP_PASS:
        log.warning("Email not configured — skipping")
        return
    try:
        # Support multiple comma-separated emails
        recipients = [e.strip() for e in ALERT_EMAIL.split(",") if e.strip()]
        if not recipients:
            log.warning("No valid email recipients found")
            return

        msg = MIMEMultipart("alternative")
        msg["Subject"] = f"📊 Multibagger Signals — {date.today().strftime('%d %b %Y')}"
        msg["From"]    = GMAIL_USER
        msg["To"]      = ", ".join(recipients)
        msg.attach(MIMEText(plain, "plain"))
        msg.attach(MIMEText(html,  "html"))
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as s:
            s.login(GMAIL_USER, GMAIL_APP_PASS)
            s.sendmail(GMAIL_USER, recipients, msg.as_string())
        log.info("Email sent ✅")
    except Exception as e:
        log.error(f"Email failed: {e}")


def send_telegram(plain: str):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT:
        log.warning("Telegram not configured — skipping")
        return
    try:
        # Support multiple comma-separated Telegram Chat IDs
        chat_ids = [c.strip() for c in str(TELEGRAM_CHAT).split(",") if c.strip()]
        if not chat_ids:
            log.warning("No valid Telegram Chat IDs found")
            return

        # Telegram has 4096 char limit — split if needed
        chunks = [plain[i:i+4000] for i in range(0, len(plain), 4000)]
        for chat_id in chat_ids:
            log.info(f"Sending Telegram alert to chat: {chat_id}...")
            for chunk in chunks:
                r = requests.post(
                    f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                    json={"chat_id": chat_id, "text": chunk, "parse_mode": ""},
                    timeout=15,
                )
                r.raise_for_status()
        log.info("Telegram sent to all chat IDs ✅")
    except Exception as e:
        log.error(f"Telegram failed: {e}")


def send_whatsapp(plain: str):
    if not TWILIO_SID or not TWILIO_TOKEN or not TWILIO_TO:
        log.warning("WhatsApp/Twilio not configured — skipping")
        return
    try:
        from twilio.rest import Client
        client = Client(TWILIO_SID, TWILIO_TOKEN)
        # WhatsApp has 1600 char limit — send summary only
        summary_lines = plain.split("\n")[:20]
        client.messages.create(
            body="\n".join(summary_lines),
            from_=TWILIO_FROM,
            to=TWILIO_TO,
        )
        log.info("WhatsApp sent ✅")
    except Exception as e:
        log.error(f"WhatsApp failed: {e}")


# ─── MAIN ─────────────────────────────────────────────────────────────────────
def main():
    log.info("=" * 60)
    log.info(f"MULTIBAGGER SCANNER — {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    log.info("=" * 60)

    # 1. Fetch from Chartink
    stocks_df = fetch_chartink_stocks()
    if stocks_df.empty:
        log.error("No stocks from Chartink — aborting")
        return

    # 2. Score each stock
    results_df = run_scanner(stocks_df)
    if results_df.empty:
        log.info("No signals today. No alerts sent.")
        # Still save empty CSV
        pd.DataFrame().to_csv(f"logs/signals_{date.today()}.csv")
        return

    log.info(f"\n✅ {len(results_df)} signals found today")

    # 3. Save CSV
    save_csv(results_df)

    # 4. Format report
    plain, html = format_report(results_df)
    print("\n" + plain)

    # 5. Send alerts (Disabled here to prevent duplicate alerts - they are combined at the end of the pipeline inside monit_ranker.py)
    # send_email(plain, html)
    # send_telegram(plain)
    # send_whatsapp(plain)

    log.info("Done. ✅")


if __name__ == "__main__":
    main()
