#!/opt/rename-webhook/bin/python3
"""
pfarrbrief_reminder.py
Täglicher Cronjob (18:00): prüft ob morgen Gottesdienste in Hölskofen/Paindlkofen sind.
Sendet bei Treffer eine Telegram-Erinnerung.
"""

import json
import sys
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, "/opt/rename-webhook")
from shared.secrets import load_secrets
from shared.telegram import send_telegram

GOTTESDIENSTE_FILE  = Path("/opt/rename-webhook/gottesdienste.json")
VEREINSTERMINE_FILE = Path("/opt/rename-webhook/vereinstermine.json")


def main():
    morgen         = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")
    morgen_display = (datetime.now() + timedelta(days=1)).strftime("%d.%m.%Y")

    zeilen = []

    # ── Gottesdienste ──────────────────────────────────────────────────────────
    if GOTTESDIENSTE_FILE.exists():
        try:
            data = json.loads(GOTTESDIENSTE_FILE.read_text())
            if isinstance(data, dict):
                raw = []
                for key in ("hk", "pk", "ok"):
                    raw.extend(data.get(key, []))
                # Duplikate entfernen (gleiche datum+uhrzeit+ort+art)
                seen = set()
                alle = []
                for t in raw:
                    sig = (t.get("datum"), t.get("uhrzeit"), t.get("ort"), t.get("art"))
                    if sig not in seen:
                        seen.add(sig)
                        alle.append(t)
            else:
                alle = data
            morgen_gd = [t for t in alle if t.get("datum") == morgen]
            if morgen_gd:
                zeilen.append("⛪ Gottesdienst morgen!\n")
                for t in morgen_gd:
                    zeilen.append(f"📅 {morgen_display}, {t['uhrzeit']} Uhr")
                    zeilen.append(f"📍 {t['ort']}")
                    zeilen.append(f"ℹ️ {t['art']}\n")
        except Exception as e:
            print(f"Fehler Gottesdienste: {e}")

    # ── Vereinstermine ─────────────────────────────────────────────────────────
    if VEREINSTERMINE_FILE.exists():
        try:
            data = json.loads(VEREINSTERMINE_FILE.read_text())
            verein_labels = {"ff": "FF Hölskofen", "kp": "Königstreue Patrioten"}
            morgen_verein = []
            for key, label in verein_labels.items():
                for t in data.get(key, []):
                    if t.get("datum") == morgen:
                        morgen_verein.append((label, t))
            if morgen_verein:
                zeilen.append("🏘️ Vereinstermin morgen!\n")
                for label, t in morgen_verein:
                    uhrzeit = f", {t['uhrzeit']} Uhr" if t.get("uhrzeit") else ""
                    ort     = f"\n📍 {t['ort']}" if t.get("ort") else ""
                    zeilen.append(f"📅 {morgen_display}{uhrzeit} – {label}")
                    zeilen.append(f"ℹ️ {t.get('bezeichnung', '')}{ort}\n")
        except Exception as e:
            print(f"Fehler Vereinstermine: {e}")

    if not zeilen:
        print(f"Keine Termine morgen ({morgen}) – nichts gesendet.")
        return

    secrets  = load_secrets()
    tg_token = secrets["TOKEN"]
    chat_id  = secrets["CHAT_ID"]

    send_telegram(tg_token, chat_id, "\n".join(zeilen))
    print(f"✅ Erinnerung gesendet")


if __name__ == "__main__":
    main()
