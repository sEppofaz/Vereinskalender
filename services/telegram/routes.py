import json
import os
import subprocess
import threading
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path

import dropbox
from flask import Blueprint, request

from shared.flask_notify import (
    TELEGRAM_CHAT_ID,
    TELEGRAM_TOKEN,
    answer_telegram_callback,
    send_telegram,
    send_telegram_inline,
)
from shared.kalender_core import (
    GOTTESDIENSTE_FILE,
    VEREINSTERMINE_FILE,
    _do_save_import,
    _kalender_pending,
    log,
)

telegram_bp = Blueprint("telegram", __name__)

_DROPBOX_INVOICE_REFRESH_TOKEN = os.environ.get("DROPBOX_INVOICE_REFRESH_TOKEN", "")
_DROPBOX_INVOICE_APP_KEY       = os.environ.get("DROPBOX_INVOICE_APP_KEY", "")
_DROPBOX_INVOICE_APP_SECRET    = os.environ.get("DROPBOX_INVOICE_APP_SECRET", "")
_TODOS_FILE_PATH               = "/Apps/Claude/PKA/Todos.md"
_GOOGLE_MAPS_API_KEY           = os.environ.get("GOOGLE_MAPS_API_KEY", "")
_VERKEHR_ORIGIN                = "Hölskofen, Pfeffenhausen, Bayern, Deutschland"


def _fmt_dauer(sek: int) -> str:
    h, m = divmod(sek // 60, 60)
    return f"{h} Std {m} Min" if h else f"{m} Min"


def _get_verkehr(ziel: str) -> str:
    if not _GOOGLE_MAPS_API_KEY:
        return "❌ Google Maps API-Key nicht konfiguriert."
    params = urllib.parse.urlencode({
        "origin":         _VERKEHR_ORIGIN,
        "destination":    ziel,
        "departure_time": "now",
        "traffic_model":  "best_guess",
        "key":            _GOOGLE_MAPS_API_KEY,
    })
    url = f"https://maps.googleapis.com/maps/api/directions/json?{params}"
    try:
        with urllib.request.urlopen(url, timeout=15) as r:
            resp = json.loads(r.read())
        if resp["status"] != "OK":
            return f"❌ Directions API: {resp['status']} – {resp.get('error_message', '')}"
        leg     = resp["routes"][0]["legs"][0]
        normal  = leg["duration"]["value"]
        traffic = leg["duration_in_traffic"]["value"]
        dist    = leg["distance"]["value"]
    except Exception as e:
        return f"❌ Fehler: {e}"

    diff_min = max(0, (traffic - normal) // 60)
    if diff_min < 10:
        ampel, hinweis = "🟢", "Straße ist frei – normal losfahren."
    elif diff_min < 20:
        ampel, hinweis = "🟡", "Etwas Verkehr – etwas früher losfahren."
    else:
        ampel, hinweis = "🔴", f"Stau! {diff_min} Min Verzögerung – deutlich früher losfahren."

    now   = datetime.now()
    tag   = ["Mo", "Di", "Mi", "Do", "Fr", "Sa", "So"][now.weekday()]
    datum = now.strftime(f"{tag}, %d.%m.%Y %H:%M")
    zeilen = [
        f"🚗 Verkehr: Hölskofen → {ziel}",
        f"{datum} Uhr",
        "",
        f"📍 Strecke: {dist / 1000:.0f} km",
        f"⏱ Normale Fahrt:  {_fmt_dauer(normal)}",
        f"{ampel} Mit Verkehr:   {_fmt_dauer(traffic)}" + (f"  (+{diff_min} Min)" if diff_min else ""),
        "",
        f"→ {hinweis}",
    ]
    return "\n".join(zeilen)


def _get_pka_dropbox_client() -> dropbox.Dropbox:
    return dropbox.Dropbox(
        oauth2_refresh_token=_DROPBOX_INVOICE_REFRESH_TOKEN,
        app_key=_DROPBOX_INVOICE_APP_KEY,
        app_secret=_DROPBOX_INVOICE_APP_SECRET,
    )


def _save_todo(text: str) -> None:
    dbx       = _get_pka_dropbox_client()
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    new_line  = f"- [ ] {timestamp} – {text}\n"
    try:
        md, res = dbx.files_download(_TODOS_FILE_PATH)
        existing = res.content.decode("utf-8")
    except dropbox.exceptions.ApiError:
        existing = "# Todos\n\n"
    updated = existing + new_line
    dbx.files_upload(
        updated.encode("utf-8"),
        _TODOS_FILE_PATH,
        mode=dropbox.files.WriteMode.overwrite,
    )


def _run(cmd: list[str], timeout: int = 10) -> str:
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return result.stdout.strip()
    except Exception as e:
        return f"(Fehler: {e})"


def _collect_status() -> str:
    lines = []

    upgradable_raw   = _run(["apt", "list", "--upgradable"])
    upgradable_lines = [l for l in upgradable_raw.splitlines() if "/" in l]
    security_updates = [l for l in upgradable_lines if "security" in l.lower()]
    reboot_required  = Path("/var/run/reboot-required").exists()

    lines.append("=== Updates ===")
    lines.append(f"Ausstehende Updates:   {len(upgradable_lines)}")
    lines.append(f"davon Security:        {len(security_updates)}")
    lines.append(f"Neustart erforderlich: {'JA ⚠️' if reboot_required else 'Nein'}")
    if security_updates:
        lines.append("Security-Pakete:")
        for pkg in security_updates[:10]:
            lines.append(f"  • {pkg.split('/')[0]}")

    lines.append("\n=== Security (fail2ban) ===")
    f2b_status = _run(["fail2ban-client", "status"])
    if "ERROR" in f2b_status or "Fehler" in f2b_status:
        lines.append("fail2ban: nicht verfügbar")
    else:
        jails_line = next((l for l in f2b_status.splitlines() if "Jail list" in l), "")
        jails = [j.strip() for j in jails_line.split(":")[-1].split(",") if j.strip()]
        for jail in jails:
            jail_info   = _run(["fail2ban-client", "status", jail])
            banned_line = next((l for l in jail_info.splitlines() if "Currently banned" in l), "")
            total_line  = next((l for l in jail_info.splitlines() if "Total banned" in l), "")
            lines.append(f"Jail '{jail}':")
            if banned_line: lines.append(f"  {banned_line.strip()}")
            if total_line:  lines.append(f"  {total_line.strip()}")

    lines.append("\n=== Letzte SSH-Logins ===")
    lines.append(_run(["last", "-n", "5", "-F"]) or "(keine Einträge)")

    lines.append("\n=== Service rename-webhook ===")
    lines.append(f"Status: {_run(['systemctl', 'is-active', 'rename-webhook'])}")

    return "\n".join(lines)


def _collect_pfarrbrief_termine(bereich: str = "hk+pk") -> str:
    gf = GOTTESDIENSTE_FILE
    if not gf.exists():
        return "⛪ Keine Gottesdienste gespeichert."
    try:
        data = json.loads(gf.read_text())
        if isinstance(data, dict):
            if bereich == "hk+pk":
                termine = data.get("hk", []) + data.get("pk", []) + data.get("hk_pk", [])
            else:
                termine = data.get(bereich, [])
        else:
            termine = data
    except Exception:
        return "⛪ Fehler beim Lesen der Gottesdienste."

    heute    = datetime.now().strftime("%Y-%m-%d")
    kuenftige = sorted(
        [t for t in termine if t.get("datum", "") >= heute],
        key=lambda t: (t["datum"], t["uhrzeit"])
    )

    labels = {"hk+pk": "Hölskofen & Paindlkofen", "hk": "Hölskofen", "pk": "Paindlkofen", "ok": "Oberköllnbach"}
    label  = labels.get(bereich, bereich)
    if not kuenftige:
        return f"⛪ Keine bevorstehenden Termine in {label}."

    zeilen = [f"⛪ Bevorstehende Termine – {label}:\n"]
    for t in kuenftige:
        datum = datetime.strptime(t["datum"], "%Y-%m-%d").strftime("%d.%m.%Y")
        zeilen.append(f"📅 {datum}, {t['uhrzeit']} Uhr")
        zeilen.append(f"📍 {t['ort']}")
        zeilen.append(f"ℹ️ {t['art']}\n")
    return "\n".join(zeilen)


def _collect_verein_termine(bereich: str = "alle") -> str:
    if not VEREINSTERMINE_FILE.exists():
        return "📅 Keine Vereinstermine gespeichert."
    try:
        data = json.loads(VEREINSTERMINE_FILE.read_text())
    except Exception:
        return "📅 Fehler beim Lesen der Vereinstermine."

    heute         = datetime.now().strftime("%Y-%m-%d")
    verein_labels = {"ff": "FF Hölskofen", "kp": "Königstreue Patrioten Hölskofen"}

    if bereich == "alle":
        termine = [dict(t, _verein="ff") for t in data.get("ff", [])] + \
                  [dict(t, _verein="kp") for t in data.get("kp", [])]
        label   = "Alle Vereine"
    else:
        termine = data.get(bereich, [])
        label   = verein_labels.get(bereich, bereich.upper())

    kuenftige = sorted(
        [t for t in termine if t.get("datum", "") >= heute],
        key=lambda t: (t["datum"], t.get("uhrzeit", ""))
    )

    if not kuenftige:
        return f"📅 Keine bevorstehenden Termine – {label}."

    zeilen = [f"📅 Bevorstehende Termine – {label}:\n"]
    for t in kuenftige:
        datum   = datetime.strptime(t["datum"], "%Y-%m-%d").strftime("%d.%m.%Y")
        uhrzeit = f", {t['uhrzeit']} Uhr" if t.get("uhrzeit") else ""
        ort     = f"\n📍 {t['ort']}" if t.get("ort") else ""
        verein  = f" [{verein_labels.get(t['_verein'], '')}]" if "_verein" in t else ""
        zeilen.append(f"• {datum}{uhrzeit} – {t.get('bezeichnung', '')}{verein}{ort}\n")
    return "\n".join(zeilen)


def _collect_alle_termine_30() -> str:
    heute      = datetime.now().date()
    ende       = heute + timedelta(days=30)
    heute_str  = heute.strftime("%Y-%m-%d")
    ende_str   = ende.strftime("%Y-%m-%d")
    alle       = []

    gf = GOTTESDIENSTE_FILE
    if gf.exists():
        try:
            raw     = json.loads(gf.read_text())
            bereiche = raw if isinstance(raw, dict) else {"alle": raw}
            seen    = set()
            for items in bereiche.values():
                for t in items:
                    datum = t.get("datum", "")
                    if not (heute_str <= datum <= ende_str):
                        continue
                    key = (datum, t.get("uhrzeit", ""), t.get("art", ""), t.get("ort", ""))
                    if key in seen:
                        continue
                    seen.add(key)
                    alle.append({
                        "datum":       datum,
                        "uhrzeit":     t.get("uhrzeit", ""),
                        "bezeichnung": t.get("art", ""),
                        "ort":         t.get("ort", ""),
                        "typ":         "⛪",
                    })
        except Exception as e:
            log(f"⚠️  Gottesdienste lesen: {e}")

    if VEREINSTERMINE_FILE.exists():
        try:
            raw           = json.loads(VEREINSTERMINE_FILE.read_text())
            verein_labels = {"ff": "FF Hölskofen", "kp": "Königstreue Patrioten Hölskofen"}
            for key, items in raw.items():
                vlabel = verein_labels.get(key, key.upper())
                for t in items:
                    datum = t.get("datum", "")
                    if not (heute_str <= datum <= ende_str):
                        continue
                    alle.append({
                        "datum":       datum,
                        "uhrzeit":     t.get("uhrzeit", ""),
                        "bezeichnung": f"{t.get('bezeichnung', '')} [{vlabel}]",
                        "ort":         t.get("ort", ""),
                        "typ":         "📅",
                    })
        except Exception as e:
            log(f"⚠️  Vereinstermine lesen: {e}")

    if not alle:
        return f"📅 Keine Termine in den nächsten 30 Tagen ({heute.strftime('%d.%m.')} – {ende.strftime('%d.%m.%Y')})."

    alle.sort(key=lambda t: (t["datum"], t.get("uhrzeit", "")))

    zeilen = [f"📅 Termine ({heute.strftime('%d.%m.')} – {ende.strftime('%d.%m.%Y')}):\n"]
    for t in alle:
        datum   = datetime.strptime(t["datum"], "%Y-%m-%d").strftime("%d.%m.%Y")
        uhrzeit = f", {t['uhrzeit']} Uhr" if t.get("uhrzeit") else ""
        ort     = f"\n📍 {t['ort']}" if t.get("ort") else ""
        zeilen.append(f"{t['typ']} {datum}{uhrzeit} – {t['bezeichnung']}{ort}\n")
    return "\n".join(zeilen)


@telegram_bp.route("/telegram", methods=["POST"])
def telegram_webhook():
    data    = request.get_json(silent=True) or {}
    log(f"[tg] update_id={data.get('update_id')} keys={list(data.keys())}")
    message = data.get("message", {})
    chat_id = str(message.get("chat", {}).get("id", ""))
    text    = message.get("text", "").strip()

    if TELEGRAM_CHAT_ID and chat_id != TELEGRAM_CHAT_ID:
        return "", 200

    if text == "/status":
        log(f"📊  /status angefordert von Chat {chat_id}")
        threading.Thread(target=lambda: send_telegram(chat_id, _collect_status()), daemon=True).start()

    elif text.lower() == "/pfarrbrief":
        log(f"⛪  /Pfarrbrief angefordert von Chat {chat_id}")
        threading.Thread(target=lambda: send_telegram(chat_id, _collect_pfarrbrief_termine("hk+pk")), daemon=True).start()

    elif text.lower() == "/pfarrbrief-hk":
        log(f"⛪  /Pfarrbrief-hk angefordert von Chat {chat_id}")
        threading.Thread(target=lambda: send_telegram(chat_id, _collect_pfarrbrief_termine("hk")), daemon=True).start()

    elif text.lower() == "/pfarrbrief-pk":
        log(f"⛪  /Pfarrbrief-pk angefordert von Chat {chat_id}")
        threading.Thread(target=lambda: send_telegram(chat_id, _collect_pfarrbrief_termine("pk")), daemon=True).start()

    elif text.lower() == "/pfarrbrief-ok":
        log(f"⛪  /Pfarrbrief-ok angefordert von Chat {chat_id}")
        threading.Thread(target=lambda: send_telegram(chat_id, _collect_pfarrbrief_termine("ok")), daemon=True).start()

    elif text.lower() == "/verein":
        log(f"📅  /verein angefordert von Chat {chat_id}")
        threading.Thread(target=lambda: send_telegram(chat_id, _collect_verein_termine("alle")), daemon=True).start()

    elif text.lower() == "/verein-ff":
        log(f"📅  /verein-ff angefordert von Chat {chat_id}")
        threading.Thread(target=lambda: send_telegram(chat_id, _collect_verein_termine("ff")), daemon=True).start()

    elif text.lower() == "/verein-kp":
        log(f"📅  /verein-kp angefordert von Chat {chat_id}")
        threading.Thread(target=lambda: send_telegram(chat_id, _collect_verein_termine("kp")), daemon=True).start()

    elif text.lower() == "/termine-30":
        log(f"📅  /Termine-30 angefordert von Chat {chat_id}")
        threading.Thread(target=lambda: send_telegram(chat_id, _collect_alle_termine_30()), daemon=True).start()

    elif text.lower() == "/help":
        log(f"❓  /help angefordert von Chat {chat_id}")
        help_text = (
            "🤖 Verfügbare Befehle:\n\n"
            "/status — Server-Status (Updates, Security, RAM, Disk)\n"
            "/update — Sicherheitsupdates jetzt einspielen\n"
            "/sicherheitscheck — Sicherheitscheck jetzt ausführen\n"
            "/reboot — Server neu starten (meldet sich wenn wieder online)\n"
            "/help — Diese Hilfe\n\n"
            "⛪ Gottesdienste:\n"
            "/pfarrbrief — Hölskofen & Paindlkofen\n"
            "/pfarrbrief-hk — nur Hölskofen\n"
            "/pfarrbrief-pk — nur Paindlkofen\n"
            "/pfarrbrief-ok — Oberköllnbach\n\n"
            "📅 Vereine:\n"
            "/verein — Alle Vereinstermine\n"
            "/verein-ff — FF Hölskofen\n"
            "/verein-kp — Königstreue Patrioten\n"
            "/termine-30 — Alle Termine (nächste 30 Tage)\n\n"
            "🚗 Verkehr:\n"
            "/verkehr <Adresse> — Verkehrsinfo Hölskofen → Ziel\n\n"
            "🏡 heimat-info:\n"
            "/heimat — Termine aller Gemeinden importieren\n"
            "/heimat-add <url> — Neue Gemeinde hinzufügen\n"
            "/heimat-excel — Bearbeitete Excel von Dropbox einlesen\n\n"
            "🔴 Vereinskalender:\n"
            "/stopp-vko — Kalender deaktivieren (Wartungsseite)\n"
            "/start-vko — Kalender wieder aktivieren\n\n"
            "💡 Ohne Befehl: Nachricht wird als Todo gespeichert"
        )
        send_telegram(chat_id, help_text)

    elif text.lower() == "/update":
        log(f"📦  /update angefordert von Chat {chat_id}")
        send_telegram(chat_id, "📦 Sicherheitsupdates werden eingespielt… (kann 1–2 Min. dauern)")
        threading.Thread(
            target=lambda: subprocess.run(
                ["sudo", "bash", "/etc/cron.weekly/sicherheitsupdates"],
                timeout=300
            ),
            daemon=True,
        ).start()

    elif text.lower() == "/sicherheitscheck":
        log(f"🔒  /sicherheitscheck angefordert von Chat {chat_id}")
        send_telegram(chat_id, "🔒 Sicherheitscheck läuft…")
        threading.Thread(
            target=lambda: subprocess.run(
                ["sudo", "bash", "/etc/pka/security_check.sh"],
                timeout=120
            ),
            daemon=True,
        ).start()

    elif text.lower().startswith("/verkehr"):
        ziel = text[8:].strip()
        if not ziel:
            send_telegram(chat_id, "Bitte Zieladresse angeben:\n/verkehr Marienplatz 1, München")
        else:
            log(f"🚗  /verkehr → {ziel[:60]}")
            send_telegram(chat_id, "🚗 Route wird berechnet…")
            threading.Thread(target=lambda z=ziel, cid=chat_id: send_telegram(cid, _get_verkehr(z)), daemon=True).start()

    elif text.lower() == "/heimat":
        log(f"🏡  /heimat Import angefordert von Chat {chat_id}")
        send_telegram(chat_id, "🏡 heimat-info Import wird gestartet…")
        threading.Thread(
            target=lambda: subprocess.run(
                ["/opt/rename-webhook/bin/python3", "/opt/rename-webhook/heimat_import.py"],
                timeout=120
            ),
            daemon=True,
        ).start()

    elif text.lower().startswith("/heimat-add"):
        url = text[11:].strip()
        if not url or not url.startswith("http"):
            send_telegram(chat_id, "Bitte URL angeben:\n/heimat-add https://www.gemeinde-xyz.de/veranstaltungen/")
        else:
            log(f"🏡  /heimat-add {url[:60]}")
            send_telegram(chat_id, f"🔍 Suche heimat-info ID für:\n{url}\n(20–30 Sek. …)")
            threading.Thread(
                target=lambda u=url: subprocess.run(
                    ["/opt/rename-webhook/bin/python3", "/opt/rename-webhook/heimat_import.py", "--add", u],
                    timeout=90
                ),
                daemon=True,
            ).start()

    elif text.lower() == "/heimat-excel":
        log(f"📊  /heimat-excel angefordert von Chat {chat_id}")
        send_telegram(chat_id, "📊 Lese heimat_preview.xlsx von Dropbox…")
        threading.Thread(
            target=lambda: subprocess.run(
                ["/opt/rename-webhook/bin/python3", "/opt/rename-webhook/heimat_import.py", "--excel"],
                timeout=120
            ),
            daemon=True,
        ).start()

    elif text.lower() == "/stopp-vko":
        log(f"🔴  /stopp-vko angefordert von Chat {chat_id}")
        _mf = Path("/opt/rename-webhook/vko_maintenance")
        _mf.touch()
        send_telegram(chat_id, "🔴 Vereinskalender deaktiviert.\nBesucher sehen jetzt die Wartungsseite.\n\n/start-vko zum Reaktivieren.")

    elif text.lower() == "/start-vko":
        log(f"🟢  /start-vko angefordert von Chat {chat_id}")
        _mf = Path("/opt/rename-webhook/vko_maintenance")
        _mf.unlink(missing_ok=True)
        send_telegram(chat_id, "🟢 Vereinskalender wieder aktiv.")

    elif text.lower() == "/reboot":
        log(f"🔄  /reboot angefordert von Chat {chat_id}")
        send_telegram(chat_id, "🔄 Server wird neu gestartet… Ich melde mich wenn er wieder online ist.")
        threading.Thread(target=lambda: subprocess.run(["sudo", "reboot"], timeout=30), daemon=True).start()

    elif text and not text.startswith("/"):
        log(f"📝  Todo von Chat {chat_id}: {text[:60]}")
        def _do_todo(t=text, cid=chat_id):
            try:
                _save_todo(t)
                send_telegram(cid, f"✅ Todo gespeichert:\n{t}")
            except Exception as e:
                log(f"❌  Todo speichern fehlgeschlagen: {e}")
                send_telegram(cid, f"❌ Todo speichern fehlgeschlagen: {e}")
        threading.Thread(target=_do_todo, daemon=True).start()

    # ── Inline-Keyboard Callbacks (Kalender-Input-Bestätigung) ────────────────
    callback_query = data.get("callback_query", {})
    if callback_query:
        cb_id   = callback_query.get("id", "")
        cb_data = callback_query.get("data", "")
        cb_chat = str(callback_query.get("from", {}).get("id", ""))

        if TELEGRAM_CHAT_ID and cb_chat != TELEGRAM_CHAT_ID:
            answer_telegram_callback(cb_id)
            return "", 200

        if cb_data.startswith("kal_ok:") or cb_data.startswith("kal_no:"):
            uid     = cb_data.split(":", 1)[1]
            pending = _kalender_pending.pop(uid, None)

            if pending is None:
                answer_telegram_callback(cb_id, "⚠️ Anfrage nicht mehr verfügbar (Server-Neustart?)")
                return "", 200

            if cb_data.startswith("kal_ok:"):
                def _do_import(p=pending):
                    try:
                        data_db = json.loads(VEREINSTERMINE_FILE.read_text()) if VEREINSTERMINE_FILE.exists() else {}
                        result_vereine, total = _do_save_import(p["alle"], p["auto_plz"], "", data_db)
                        namen = ", ".join(v["name"] for v in result_vereine[:3])
                        if len(result_vereine) > 3:
                            namen += f" (+{len(result_vereine)-3})"
                        send_telegram(TELEGRAM_CHAT_ID,
                            f"✅ Import abgeschlossen: {p['dateiname']}\n"
                            f"📋 {total} Termine · {len(result_vereine)} Vereine\n"
                            f"🏷 {namen}")
                    except Exception as e:
                        log(f"❌  Kalender-Import nach Bestätigung fehlgeschlagen: {e}")
                        send_telegram(TELEGRAM_CHAT_ID, f"❌ Import fehlgeschlagen: {e}")
                answer_telegram_callback(cb_id, "⏳ Importiere…")
                threading.Thread(target=_do_import, daemon=True).start()
            else:
                answer_telegram_callback(cb_id, "🗑 Verworfen")
                send_telegram(TELEGRAM_CHAT_ID, f"🗑 Verworfen: {pending['dateiname']}")

        elif cb_data.startswith("heimat_ok:") or cb_data.startswith("heimat_no:"):
            uid = cb_data.split(":", 1)[1]
            if cb_data.startswith("heimat_ok:"):
                def _do_heimat_import(u=uid):
                    import traceback
                    try:
                        import importlib.util, sys as _sys
                        spec = importlib.util.spec_from_file_location(
                            "heimat_import", "/opt/rename-webhook/heimat_import.py")
                        mod = importlib.util.module_from_spec(spec)
                        spec.loader.exec_module(mod)
                        result = mod.do_import(u)
                        log(f"[heimat] do_import result: {result!r}, token_set={bool(TELEGRAM_TOKEN)}, chat={TELEGRAM_CHAT_ID!r}")
                        send_telegram(TELEGRAM_TOKEN, TELEGRAM_CHAT_ID, result)
                        log("[heimat] send_telegram OK")
                    except Exception as e:
                        tb = traceback.format_exc()
                        log(f"❌ heimat-Import Thread-Fehler: {e}\n{tb}")
                        try:
                            send_telegram(TELEGRAM_TOKEN, TELEGRAM_CHAT_ID, f"❌ heimat-Import fehlgeschlagen: {e}")
                        except Exception as e2:
                            log(f"❌ Telegram-Senden fehlgeschlagen: {e2}")
                answer_telegram_callback(cb_id, "⏳ Importiere…")
                threading.Thread(target=_do_heimat_import, daemon=True).start()
            else:
                from pathlib import Path as _Path
                _Path(f"/tmp/heimat_pending_{uid}.json").unlink(missing_ok=True)
                answer_telegram_callback(cb_id, "🗑 Verworfen")
                send_telegram(TELEGRAM_CHAT_ID, "🗑 heimat-info Import verworfen.")

        elif cb_data.startswith("verein_approve:") or cb_data.startswith("verein_reject:"):
            from shared.vk_db import db_conn
            from shared.vk_mail import send_welcome_email, send_rejected_email
            try:
                parts = cb_data.split(":", 2)
                verein_id = int(parts[1])
                expected_name = parts[2] if len(parts) > 2 else None
                approve = cb_data.startswith("verein_approve:")
                with db_conn() as conn:
                    row = conn.execute(
                        """SELECT v.verein_name, u.email FROM vereine_accounts v
                           JOIN vk_users u ON u.verein_id = v.id AND u.role='admin'
                           WHERE v.id = ? AND v.status = 'pending'""",
                        (verein_id,),
                    ).fetchone()
                    if not row:
                        answer_telegram_callback(cb_id, "⚠️ Bereits bearbeitet")
                    elif expected_name and row["verein_name"][:30].replace(":", "_") != expected_name:
                        answer_telegram_callback(cb_id, "⚠️ ID-Kollision – Verein nicht mehr identisch. Bitte neu prüfen.")
                    else:
                        if approve:
                            conn.execute(
                                "UPDATE vereine_accounts SET status='aktiv', freigegeben_at=CURRENT_TIMESTAMP WHERE id=?",
                                (verein_id,),
                            )
                            send_welcome_email(row["email"], row["verein_name"])
                            answer_telegram_callback(cb_id, "✅ Freigegeben")
                            send_telegram(TELEGRAM_CHAT_ID, f"✅ Verein freigegeben: {row['verein_name']}")
                        else:
                            conn.execute(
                                "UPDATE vereine_accounts SET status='abgelehnt' WHERE id=?",
                                (verein_id,),
                            )
                            send_rejected_email(row["email"], row["verein_name"])
                            answer_telegram_callback(cb_id, "❌ Abgelehnt")
                            send_telegram(TELEGRAM_CHAT_ID, f"❌ Verein abgelehnt: {row['verein_name']}")
            except Exception as e:
                answer_telegram_callback(cb_id, f"❌ Fehler: {e}")

    return "", 200
