#!/opt/rename-webhook/bin/python3
import requests
import subprocess
import time

HELP_TEXT = (
    "📋 Verfügbare Befehle:\n"
    "/status – Service-Status anzeigen\n"
    "/reboot – Server neu starten\n"
    "/help – Diese Übersicht"
)

def load_config():
    cfg = {}
    with open("/etc/pka/secrets.env") as f:
        for line in f:
            line = line.strip()
            if "=" in line:
                k, v = line.split("=", 1)
                cfg[k.strip()] = v.strip()
    return cfg

cfg = load_config()
TOKEN = cfg["TOKEN"]
CHAT_ID = str(cfg["CHAT_ID"])
BASE = f"https://api.telegram.org/bot{TOKEN}"

def send(text):
    requests.post(f"{BASE}/sendMessage", data={"chat_id": CHAT_ID, "text": text}, timeout=10)

def get_updates(offset):
    try:
        r = requests.get(f"{BASE}/getUpdates", params={"offset": offset, "timeout": 30}, timeout=35)
        return r.json().get("result", [])
    except Exception:
        return []

def main():
    offset = 0
    while True:
        updates = get_updates(offset)
        for update in updates:
            offset = update["update_id"] + 1
            msg = update.get("message", {})
            chat_id = str(msg.get("chat", {}).get("id", ""))
            text = msg.get("text", "").strip()

            if chat_id != CHAT_ID:
                continue

            if text == "/reboot":
                send("🔄 Reboot wird ausgeführt...")
                time.sleep(1)
                subprocess.run(["reboot"])
            elif text == "/status":
                result = subprocess.run(
                    ["systemctl", "is-active", "rename-webhook"],
                    capture_output=True, text=True
                )
                send(f"📊 rename-webhook: {result.stdout.strip()}\n\n{HELP_TEXT}")
            elif text == "/help":
                send(HELP_TEXT)

if __name__ == "__main__":
    main()
