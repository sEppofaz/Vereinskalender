#!/opt/rename-webhook/bin/python3
"""
logbuch_summary.py
Liest den gestrigen Logbuch-Eintrag aus Dropbox und sendet ihn per Telegram.
Läuft täglich um 06:30 via Cron – nur wenn ein Eintrag vom Vortag existiert.
"""

import json
import re
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path

LOGBUCH_DROPBOX_PATH = "/Apps/Claude/PKA/Logbuch.md"


def load_secrets() -> dict:
    secrets = {}
    for line in Path("/etc/pka/secrets.env").read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        line = re.sub(r"^export\s+", "", line)
        if "=" in line:
            k, _, v = line.partition("=")
            secrets[k.strip()] = v.strip().strip('"').strip("'")
    return secrets


def get_dropbox_token(secrets: dict) -> str:
    url     = "https://api.dropboxapi.com/oauth2/token"
    payload = (
        f"grant_type=refresh_token"
        f"&refresh_token={secrets['DROPBOX_INVOICE_REFRESH_TOKEN']}"
        f"&client_id={secrets['DROPBOX_INVOICE_APP_KEY']}"
        f"&client_secret={secrets['DROPBOX_INVOICE_APP_SECRET']}"
    ).encode()
    req = urllib.request.Request(url, data=payload, method="POST")
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read())["access_token"]


def download_logbuch(token: str) -> str:
    url  = "https://content.dropboxapi.com/2/files/download"
    args = json.dumps({"path": LOGBUCH_DROPBOX_PATH})
    req  = urllib.request.Request(url, headers={
        "Authorization": f"Bearer {token}",
        "Dropbox-API-Arg": args,
    })
    with urllib.request.urlopen(req, timeout=15) as r:
        return r.read().decode("utf-8")


def extract_entry(content: str, date_str: str) -> str | None:
    """Extrahiert alle Abschnitte fuer YYYY-MM-DD (inkl. Nachtrag-Eintraege)."""
    # Matcht ## YYYY-MM-DD mit beliebigem Suffix (z.B. " (Nachtrag 3)")
    pattern = rf"^## {re.escape(date_str)}[^\n]*\n(.*?)(?=\n^## |\Z)"
    matches = re.findall(pattern, content, re.DOTALL | re.MULTILINE)
    if not matches:
        return None
    parts = [m.strip() for m in reversed(matches) if m.strip()]
    return "\n\n---\n\n".join(parts) if parts else None


def send_telegram(token: str, chat_id: str, text: str) -> None:
    url     = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = json.dumps({"chat_id": chat_id, "text": text}).encode()
    req     = urllib.request.Request(
        url, data=payload, headers={"Content-Type": "application/json"})
    urllib.request.urlopen(req, timeout=10)


def main():
    yesterday  = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
    day_german = datetime.strptime(yesterday, "%Y-%m-%d").strftime("%d.%m.%Y")

    try:
        secrets = load_secrets()
    except Exception as e:
        print(f"secrets.env Fehler: {e}")
        return

    try:
        token   = get_dropbox_token(secrets)
        content = download_logbuch(token)
    except Exception as e:
        print(f"Dropbox-Fehler: {e}")
        return

    entry = extract_entry(content, yesterday)
    if not entry:
        print(f"Kein Eintrag fuer {yesterday} – nichts gesendet.")
        return

    message = f"📋 PKA-Logbuch {day_german}\n\n{entry}"

    try:
        send_telegram(secrets["TOKEN"], secrets["CHAT_ID"], message)
        print(f"✅ Logbuch-Zusammenfassung gesendet ({yesterday})")
    except Exception as e:
        print(f"Telegram-Fehler: {e}")


if __name__ == "__main__":
    main()
