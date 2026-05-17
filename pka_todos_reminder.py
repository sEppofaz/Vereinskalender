#!/usr/bin/env python3
"""Alle 15 Min via Cron: PKA Todos mit Fälligkeit prüfen → Telegram.

- Todos MIT Uhrzeit: Erinnerung 15 Min vorher (Fenster [uhrzeit-15min, uhrzeit))
- Todos OHNE Uhrzeit: Erinnerung täglich beim 08:00-Lauf (08:00–08:14)
- Überfällige: täglich beim 08:00-Lauf
"""
import json
import os
import urllib.request
from datetime import date, datetime, timedelta

import dropbox

_REFRESH_TOKEN  = os.environ.get("DROPBOX_INVOICE_REFRESH_TOKEN", "")
_APP_KEY        = os.environ.get("DROPBOX_INVOICE_APP_KEY", "")
_APP_SECRET     = os.environ.get("DROPBOX_INVOICE_APP_SECRET", "")
_TOKEN          = os.environ.get("TOKEN", "")
_CHAT_ID        = os.environ.get("CHAT_ID", "")
_TODOS_FILE     = "/Apps/Claude/Todo-App/Todos.json"
_VORWARNUNG_MIN = 15

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
    jetzt       = datetime.now()
    heute       = jetzt.date().isoformat()
    morgen_lauf = (jetzt.hour == 8 and jetzt.minute < _VORWARNUNG_MIN)

    _, res = _dbx().files_download(_TODOS_FILE)
    todos = json.loads(res.content).get("todos", [])

    erinnerung_jetzt, faellig, ueberfaellig = [], [], []

    for t in todos:
        f  = t.get("faelligkeit")
        fu = t.get("faelligkeit_uhrzeit")  # "HH:MM" oder None
        if not f or t.get("erledigt"):
            continue

        if fu:
            target        = datetime.strptime(f"{f} {fu}", "%Y-%m-%d %H:%M")
            fenster_start = target - timedelta(minutes=_VORWARNUNG_MIN)
            if fenster_start <= jetzt < target:
                erinnerung_jetzt.append(t)
            elif jetzt >= target and morgen_lauf:
                ueberfaellig.append(t)
        else:
            if f == heute and morgen_lauf:
                faellig.append(t)
            elif f < heute and morgen_lauf:
                ueberfaellig.append(t)

    if not erinnerung_jetzt and not faellig and not ueberfaellig:
        return

    lines = []

    if erinnerung_jetzt:
        lines.append("⏰ <b>Fällig in 15 Minuten:</b>")
        for t in erinnerung_jetzt:
            p  = PRIO_EMOJI.get(t.get("prio", "niedrig"), "⚪")
            fu = t.get("faelligkeit_uhrzeit", "")
            lines.append(f"{p} {t['aufgabe']}" + (f" ({fu} Uhr)" if fu else ""))

    if faellig or ueberfaellig:
        if lines:
            lines.append("")
        lines.append("📋 <b>PKA Todo-Erinnerung</b>")
        if faellig:
            lines.append(f"\n🔔 <b>Heute fällig ({len(faellig)}):</b>")
            for t in faellig:
                p = PRIO_EMOJI.get(t.get("prio", "niedrig"), "⚪")
                lines.append(f"{p} {t['aufgabe']}")
        if ueberfaellig:
            lines.append(f"\n⚠️ <b>Überfällig ({len(ueberfaellig)}):</b>")
            for t in ueberfaellig:
                p  = PRIO_EMOJI.get(t.get("prio", "niedrig"), "⚪")
                f  = t["faelligkeit"]
                df = f"{f[8:10]}.{f[5:7]}.{f[2:4]}"
                lines.append(f"{p} {t['aufgabe']} <i>(seit {df})</i>")

    _send("\n".join(lines))


if __name__ == "__main__":
    main()
