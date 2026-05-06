import json
import os
import urllib.request

from shared.kalender_core import log

TELEGRAM_TOKEN   = os.environ.get("TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("CHAT_ID", "")


def send_telegram(chat_id: str | int, text: str) -> None:
    if not TELEGRAM_TOKEN:
        log("⚠️  TELEGRAM_TOKEN nicht gesetzt – Nachricht nicht gesendet")
        return
    url     = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = json.dumps({"chat_id": chat_id, "text": text}).encode()
    req     = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"})
    try:
        urllib.request.urlopen(req, timeout=10)
    except Exception as e:
        log(f"❌  Telegram-Sendefehler: {e}")


def send_telegram_inline(chat_id: str | int, text: str, keyboard: list) -> int | None:
    """Sendet Nachricht mit Inline-Keyboard, gibt message_id zurück."""
    if not TELEGRAM_TOKEN:
        return None
    url     = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = json.dumps({
        "chat_id":      chat_id,
        "text":         text,
        "reply_markup": {"inline_keyboard": keyboard},
    }).encode()
    req = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"})
    try:
        resp = urllib.request.urlopen(req, timeout=10)
        data = json.loads(resp.read())
        return data.get("result", {}).get("message_id")
    except Exception as e:
        log(f"❌  Telegram-Inline-Sendefehler: {e}")
        return None


def answer_telegram_callback(callback_query_id: str, text: str = "") -> None:
    """Beantwortet einen Inline-Keyboard-Callback (entfernt Lade-Spinner)."""
    if not TELEGRAM_TOKEN:
        return
    url     = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/answerCallbackQuery"
    payload = json.dumps({"callback_query_id": callback_query_id, "text": text}).encode()
    req     = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"})
    try:
        urllib.request.urlopen(req, timeout=10)
    except Exception as e:
        log(f"❌  Telegram-Callback-Fehler: {e}")
