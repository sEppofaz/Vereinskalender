#!/opt/rename-webhook/bin/python3
"""
kalender_erinnerung.py
Täglich 18:00: Sendet Telegram-Erinnerungen für morgige Termine an alle Abonnenten.
"""

import json
import sys
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, "/opt/rename-webhook")
from shared.vk_db import tg_get_all_subscriptions

VEREINSTERMINE_FILE = Path("/opt/rename-webhook/vereinstermine.json")
BOT_TOKEN_FILE      = Path("/etc/pka/secrets.env")


def load_kalender_bot_token() -> str:
    for line in BOT_TOKEN_FILE.read_text().splitlines():
        line = line.strip().lstrip("export").strip()
        if line.startswith("KALENDER_BOT_TOKEN="):
            return line.split("=", 1)[1].strip().strip('"').strip("'")
    return ""


def send(token: str, chat_id: str, text: str):
    payload = json.dumps({"chat_id": chat_id, "text": text, "parse_mode": "HTML"}).encode()
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data=payload,
        headers={"Content-Type": "application/json"},
    )
    try:
        urllib.request.urlopen(req, timeout=10)
    except Exception as e:
        print(f"  ❌ Fehler für {chat_id}: {e}")


def main():
    token = load_kalender_bot_token()
    if not token:
        print("❌ KALENDER_BOT_TOKEN nicht gefunden – abgebrochen.")
        return

    if not VEREINSTERMINE_FILE.exists():
        print("❌ vereinstermine.json nicht gefunden – abgebrochen.")
        return

    data   = json.loads(VEREINSTERMINE_FILE.read_text())
    labels = data.get("_labels", {})

    morgen    = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")
    morgen_de = (datetime.now() + timedelta(days=1)).strftime("%d.%m.%Y")
    wochentage = ["Montag","Dienstag","Mittwoch","Donnerstag","Freitag","Samstag","Sonntag"]
    morgen_wt  = wochentage[(datetime.now() + timedelta(days=1)).weekday()]

    morgen_termine: dict[str, list] = {}
    for key, termine in data.items():
        if key.startswith("_") or not isinstance(termine, list):
            continue
        treffer = [t for t in termine if t.get("datum") == morgen]
        if treffer:
            morgen_termine[key] = treffer

    if not morgen_termine:
        print(f"Keine Termine morgen ({morgen}) – nichts gesendet.")
        return

    alle_abos = tg_get_all_subscriptions()
    abos_per_chat: dict[str, list] = {}
    for abo in alle_abos:
        abos_per_chat.setdefault(abo["chat_id"], []).append(abo["verein_key"])

    gesendet = 0
    for chat_id, abonnierte_vereine in abos_per_chat.items():
        relevante = [k for k in abonnierte_vereine if k in morgen_termine]
        if not relevante:
            continue

        zeilen = [f"🔔 <b>Morgen, {morgen_wt} {morgen_de}:</b>\n"]
        for key in relevante:
            verein_name = labels.get(key, key)
            zeilen.append(f"🏘️ <b>{verein_name}</b>")
            for t in morgen_termine[key]:
                uhrzeit = f"⏰ {t['uhrzeit']} Uhr\n" if t.get("uhrzeit") else ""
                ort     = f"📍 {t['ort']}\n"         if t.get("ort")     else ""
                zeilen.append(f"📋 {t.get('bezeichnung','')}\n{uhrzeit}{ort}")

        zeilen.append("─────────────────")
        zeilen.append("Abos ändern: /abo · Abmelden: /stop")
        send(token, chat_id, "\n".join(zeilen))
        gesendet += 1

    print(f"✅ Erinnerungen gesendet: {gesendet} Nutzer, {len(morgen_termine)} Vereine mit Terminen")


if __name__ == "__main__":
    main()
