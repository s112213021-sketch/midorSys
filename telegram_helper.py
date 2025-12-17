import os
import requests
import threading

# Telegram helper: centralized place for sending messages
# Usage: from telegram_helper import send_telegram, send_telegram_async

TG_TOKEN = os.getenv("TG_TOKEN")
TG_CHAT_ID = os.getenv("TG_CHAT_ID")

def _send(text: str):
    if not TG_TOKEN or not TG_CHAT_ID:
        print("[tg] TG_TOKEN or TG_CHAT_ID not set, skipping message")
        return
    url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
    resp = requests.post(url, json={"chat_id": TG_CHAT_ID, "text": text})
    if resp.status_code != 200:
        print(f"[tg] send failed: {resp.status_code} {resp.text}")

def send_telegram(text: str):
    """Send a Telegram message synchronously. Exceptions propagate to caller.
    The function logs failures to stdout.
    """
    try:
        _send(text)
    except Exception as e:
        # Keep behaviour visible to caller via printed message
        print(f"[tg] exception: {e}")

def send_telegram_async(text: str):
    """Send a Telegram message in background thread (non-blocking)."""
    threading.Thread(target=send_telegram, args=(text,), daemon=True).start()
