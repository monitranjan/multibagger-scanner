#!/usr/bin/env python3
"""
Test Telegram Bot Configuration
───────────────────────────────
Quick utility script to load your local .env file, verify your Telegram Bot token,
and send a test message to ensure the Chat ID is correct and active.
"""

import os
import requests

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
    else:
        print("⚠️ Warning: No .env file found in this directory!")

print("🔍 Loading local .env file...")
load_dotenv()

token = os.environ.get("TELEGRAM_TOKEN", "")
chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")

print(f"• TELEGRAM_TOKEN:   {token[:10]}...{token[-5:] if len(token) > 10 else ''}" if token else "• TELEGRAM_TOKEN:   ❌ NOT SET")
print(f"• TELEGRAM_CHAT_ID: {chat_id}" if chat_id else "• TELEGRAM_CHAT_ID: ❌ NOT SET")
print("-" * 50)

if not token or not chat_id:
    print("❌ Error: Missing configuration!")
    print("Please open the '.env' file in your folder, fill in your actual credentials, and save it.")
    exit(1)

# Step 1: Verify Bot Token
print("🤖 Step 1: Verifying Bot Token with Telegram...")
try:
    r = requests.get(f"https://api.telegram.org/bot{token}/getMe", timeout=10)
    if r.status_code == 200:
        bot_info = r.json().get("result", {})
        print(f"✅ Bot Token is VALID!")
        print(f"   - Name: {bot_info.get('first_name')}")
        print(f"   - Username: @{bot_info.get('username')}")
    else:
        print(f"❌ Bot Token is INVALID (Status Code: {r.status_code})!")
        print("Please double check your TELEGRAM_TOKEN in '.env'. It should look like '123456789:ABCdef...'")
        exit(1)
except Exception as e:
    print(f"❌ Connection failed: {e}")
    exit(1)

print("-" * 50)

# Step 2: Send Test Message
chat_ids = [c.strip() for c in str(chat_id).split(",") if c.strip()]
print(f"📨 Step 2: Sending test message to {len(chat_ids)} Chat ID(s)...")

for c_id in chat_ids:
    print(f"\n👉 Testing Chat ID: '{c_id}'...")
    try:
        payload = {
            "chat_id": c_id,
            "text": f"🚀 Hello from your local Monit Momentum Bot! Your credentials for Chat ID '{c_id}' are working perfectly! 🎉",
            "parse_mode": "Markdown"
        }
        r = requests.post(f"https://api.telegram.org/bot{token}/sendMessage", json=payload, timeout=10)
        
        if r.status_code == 200:
            print("   🎉 SUCCESS! Test message sent successfully!")
        else:
            resp_json = r.json()
            error_desc = resp_json.get("description", "Unknown error")
            print(f"   ❌ Telegram API returned error code {r.status_code}: {error_desc}")
            print("\n   💡 Troubleshooting Checklist:")
            
            # Check if chat ID looks like a username
            if str(c_id).startswith("@"):
                print("   👉 CRITICAL: You entered a username (starting with '@') as your chat ID.")
                print("      Bots cannot message personal usernames directly. You MUST use your numerical ID.")
                print("      Use '@userinfobot' in Telegram to get your numerical chat ID (e.g. 987654321).")
            
            # Check if 400 Bad Request
            elif r.status_code == 400:
                print("   👉 1. Have you started the chat with the bot?")
                print(f"         Search for '@{bot_info.get('username')}' in Telegram and click 'Start' at the bottom.")
                print("         Bots are strictly blocked from messaging users who haven't started the chat first!")
                print("\n   👉 2. Is the numerical Chat ID correct?")
                print("         Go to Telegram, search for '@userinfobot' (or '@getmyid_bot'), click Start,")
                print("         and copy the 'Id' number exactly into your '.env' file.")
    except Exception as e:
        print(f"   ❌ Message sending failed: {e}")
