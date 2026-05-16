#!/usr/bin/env python3
"""Täglich 08:00 via Cron: PKA Todos mit Fälligkeit heute oder überfällig → Telegram."""
import json
import os
import urllib.request
from datetime import date

import dropbox

_REFRESH_TOKEN = os.environ.get("DROPBOX_INVOICE_REFRESH_TOKEN", "")
_APP_KEY       = os.environ.get("DROPBOX_INVOICE_APP_KEY", "")
_APP_SECRET    = os.environ.get("DROPBOX_INVOICE_APP_SECRET", "")
_TOKEN         = os.environ.get("TOKEN", "")
_CHAT_ID       = os.environ.get("CHAT_ID", "")
_TODOS_FILE    = "/Apps/Claude/PKA/Todos.json"

PRIO_EMOJI = {"hoch": "🔴", "mittel": "🟡", "niedrig": "⚪"}


def _dbx():
    return dropbox.Dropbox(
        oauth2_refresh_token=_REFRESH_TOKEN,
        app_key=_APP_KEY,
        app_secret=_APP_SECRET,
    )


def _send(text: str) -> None:
    url  = f"https://api.telegram.org/bot{_TOKEN}/sendMessage"
    body = json.dumps({"chat_id": _CHAT_ID, "text": text, "parse_mode": "HTML"}).encode()
    req  = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
    urllib.request.urlopen(req, timeout=10)


def main() -> None:
    today = date.today().isoformat()
    _, res = _dbx().files_download(_TODOS_FILE)
    todos = json.loads(res.content).get("todos", [])

    faellig, ueberfaellig = [], []
    for t in todos:
        f = t.get("faelligkeit")
        if not f or t.get("erledigt"):
            continue
        if f == today:
            faellig.append(t)
        elif f < today:
            ueberfaellig.append(t)

    if not faellig and not ueberfaellig:
        return

    lines = ["📋 <b>PKA Todo-Erinnerung</b>"]

    if faellig:
        lines.append(f"\n🔔 <b>Heute fällig ({len(faellig)}):</b>")
        for t in faellig:
            p = PRIO_EMOJI.get(t.get("prio", "niedrig"), "⚪")
            lines.append(f"{p} {t['aufgabe']}")

    if ueberfaellig:
        lines.append(f"\n⚠️ <b>Überfällig ({len(ueberfaellig)}):</b>")
        for t in ueberfaellig:
            p   = PRIO_EMOJI.get(t.get("prio", "niedrig"), "⚪")
            f   = t["faelligkeit"]
            df  = f"{f[8:10]}.{f[5:7]}.{f[2:4]}"
            lines.append(f"{p} {t['aufgabe']} <i>(seit {df})</i>")

    _send("\n".join(lines))


if __name__ == "__main__":
    main()
