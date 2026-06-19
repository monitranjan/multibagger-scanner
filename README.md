# 📊 TIMELOSS Multibagger Scanner

Automated daily stock scanner that:
1. Fetches **300–400 momentum stocks** from your Chartink screener
2. Applies your **Pine Script strategy logic** (200 EMA / RSI / ATH) in Python
3. Ranks them and sends **top 20–25 signals** via Email + Telegram + WhatsApp
4. Runs **every weekday at 3:35 PM IST** on GitHub — zero cost, no laptop needed

---

## 🗂 Project structure

```
multibagger-scanner/
├── scanner.py                      ← main script (all logic lives here)
├── requirements.txt
├── .github/
│   └── workflows/
│       └── daily_scan.yml          ← GitHub Actions schedule
├── logs/
│   ├── scanner.log                 ← run logs
│   └── signals_YYYY-MM-DD.csv     ← daily results
└── README.md
```

---

## 🚀 One-time setup (30 minutes total)

### Step 1 — Create GitHub repo

1. Go to https://github.com/new
2. Name it `multibagger-scanner`, set to **Private**
3. Click **Create repository**
4. Upload all files from this folder into it

---

### Step 2 — Set up Gmail App Password

> This lets the script send email without exposing your real password.

1. Go to https://myaccount.google.com/security
2. Enable **2-Step Verification** if not already on
3. Search "App passwords" → Create one → name it "Scanner"
4. Copy the 16-character password shown (you won't see it again)

---

### Step 3 — Set up Telegram bot (5 minutes)

1. Open Telegram → search **@BotFather** → `/newbot`
2. Give it a name (e.g. `MultibaggerBot`) and username (e.g. `my_multibagger_bot`)
3. Copy the **bot token** (looks like `7123456789:AAHxxxx...`)
4. Start a chat with your new bot (just send `/start`)
5. Get your **Chat ID**: open this URL in browser (replace YOUR_TOKEN):
   ```
   https://api.telegram.org/botYOUR_TOKEN/getUpdates
   ```
   Look for `"chat":{"id":XXXXXXXXX}` — that number is your Chat ID

---

### Step 4 — Set up WhatsApp via Twilio (optional)

> Free tier gives 1 free WhatsApp number. Uses Twilio Sandbox.

1. Sign up at https://www.twilio.com (free)
2. Go to **Messaging → Try it out → Send a WhatsApp message**
3. Send the join code from your WhatsApp to `+1 415 523 8886`
4. Note your **Account SID** and **Auth Token** from the Twilio dashboard
5. Your `TWILIO_WHATSAPP_TO` is `whatsapp:+91XXXXXXXXXX` (your number with country code)

---

### Step 5 — Add GitHub Secrets

> This is how you store passwords securely — GitHub encrypts them.

1. In your GitHub repo → **Settings → Secrets and variables → Actions**
2. Click **New repository secret** for each one below:

| Secret name            | Value                              | Required? |
|------------------------|------------------------------------|-----------|
| `GMAIL_USER`           | your.email@gmail.com               | ✅ Yes    |
| `GMAIL_APP_PASS`       | 16-char app password from Step 2   | ✅ Yes    |
| `ALERT_EMAIL`          | email to receive alerts (same/diff)| ✅ Yes    |
| `TELEGRAM_TOKEN`       | Bot token from Step 3              | ✅ Yes    |
| `TELEGRAM_CHAT_ID`     | Chat ID number from Step 3         | ✅ Yes    |
| `TWILIO_SID`           | Account SID from Twilio            | Optional  |
| `TWILIO_TOKEN`         | Auth Token from Twilio             | Optional  |
| `TWILIO_WHATSAPP_FROM` | `whatsapp:+14155238886`            | Optional  |
| `TWILIO_WHATSAPP_TO`   | `whatsapp:+91XXXXXXXXXX`           | Optional  |

---

### Step 6 — Test it manually

1. In your GitHub repo → **Actions** tab
2. Click **Daily Multibagger Scan** → **Run workflow** → **Run workflow**
3. Watch the logs in real time
4. Check your email + Telegram in ~5 minutes

---

## 📊 Understanding the signals

| Signal | What it means | Allocation |
|--------|--------------|------------|
| 🟢 **EMA Crossover** | Price crossed above 200 EMA today + RSI > 60 + within 30% ATH | 7% of capital |
| 🟡 **52W Breakout** | New 52-week high above 200 EMA + RSI > 60 + monthly momentum | 5% of capital |
| 🔵 **ATH Momentum** | Already trending, within 15% of ATH, RSI > 60, weekly + monthly momentum | 4% of capital |

**With ₹10L capital and 20–25 positions:**
- EMA Crossover → ~₹70,000 per stock
- 52W Breakout → ~₹50,000 per stock
- ATH Momentum → ~₹40,000 per stock

### 📈 Conviction & Delivery Signals (NSE Live Volume)

These signals are computed dynamically at run-time by evaluating live NSE deliverable positions and 5-day moving statistics:

| Signal | Mathematical Definition / Condition | Investment Conviction & Meaning |
|:---|:---|:---|
| **🔥 High Accumulation** | `latest_delivery_pct > 5-day median + 5%` and<br>`latest_delivery_qty > 5-day median * 1.2` and<br>`demat_delivery_value >= ₹1.0 Cr` | Heavy institutional or insider buying. Excellent setup for accumulating or holding. |
| **🛡️ Strong Delivery** | `latest_delivery_pct >= 45%` OR<br>(`latest_delivery_pct > 5-day median + 2%` and<br>`latest_delivery_qty >= 5-day median`) | Steady buying pressure with shares being tucked away in Demat. Supports holding or adding. |
| **⚠️ Speculative Churn** | `latest_traded_qty > 5-day median * 2.0` and<br>`latest_delivery_pct < 20%` | High intraday volatility and day-trading churn with very low long-term conviction. Caution recommended. |
| **⚖️ Neutral** | Fallback when none of the rules above are met. | Volume and delivery are in line with weekly averages. Rely on primary technical confluences. |

---

## 🚪 Exit rules (check manually on TradingView)

Your Pine Script has two exits — watch for these manually:
- **EMA Exit**: Stock stays below 200 EMA for 65 consecutive days → sell
- **Time Stop**: Less than 10% return after 90 days → sell and redeploy

> 💡 Tip: Set a TradingView alert on your holdings for "Close < 200 EMA" so you get notified.

---

## ⚙️ Customising the script

All key parameters are at the top of `scanner.py`:

```python
RSI_MIN        = 60     # increase to 65 for stricter filter
ATH_THRESHOLD1 = 30     # % below ATH for EMA Crossover and 52W Breakout entries
ATH_THRESHOLD2 = 15     # % below ATH for ATH Momentum entry
MAX_POSITIONS  = 22     # max stocks in the output

POSITION_SIZE = {
    "EMA Crossover": 0.07,   # 7% of ₹10L = ₹70,000
    "52W Breakout":  0.05,   # 5% of ₹10L = ₹50,000
    "ATH Momentum":  0.04,   # 4% of ₹10L = ₹40,000
}
```

---

## ❓ Troubleshooting

**No stocks returned from Chartink**
→ Chartink may have changed their CSRF mechanism. Open an issue.

**yfinance returning empty data**
→ NSE codes in Chartink may differ from Yahoo Finance. The script tries `.NS` then `.BO` automatically.

**GitHub Action not running at 3:35 PM**
→ GitHub Actions cron can be delayed by 5–30 min during high load. This is normal.

**Email going to spam**
→ Add your Gmail to your contacts. Or use a dedicated alerts Gmail account.

---

## 📝 Disclaimer

This tool is for educational and research purposes only.
It is not financial advice. Stock market investments carry risk.
Always do your own research and consult a SEBI-registered advisor before investing.
