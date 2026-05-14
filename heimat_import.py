#!/opt/rename-webhook/bin/python3
"""
heimat_import.py
Fetcht Events von heimat-info.de für alle konfigurierten Gemeinden
und sendet Telegram-Vorschau mit ✅/❌ Import-Buttons.

Aufruf:
  python3 heimat_import.py              → Import-Vorschau per Telegram
  python3 heimat_import.py --add <url>  → Neue Gemeinde via Playwright entdecken
"""
import html as htmlmod
import json
import re
import sys
import urllib.request
import uuid
from datetime import datetime
from pathlib import Path

sys.path.insert(0, "/opt/rename-webhook")
from shared.secrets import load_secrets
from shared.telegram import send_telegram, send_telegram_inline

GEMEINDEN_FILE      = Path("/opt/rename-webhook/heimat_gemeinden.json")
VEREINSTERMINE_FILE = Path("/opt/rename-webhook/vereinstermine.json")
LOG_FILE            = "/var/log/pka-heimat.log"
API_BASE            = "https://www.heimat-info.de/embeddings/events/v1/"

MONATE     = {"Januar":1,"Februar":2,"März":3,"April":4,"Mai":5,"Juni":6,
               "Juli":7,"August":8,"September":9,"Oktober":10,"November":11,"Dezember":12}
KATEGORIEN = {"Vereine","Kirchen","Feuerwehren","Gastro / Gewerbe",
               "Veranstaltungen","Sport","Kultur","Sonstiges","Gemeinde","Freizeit"}
WOCHENTAGE = {"So.","Sa.","Mo.","Di.","Mi.","Do.","Fr."}
SKIP_TEXT  = {"zum Kalender hinzufügen","mehr anzeigen"}


def _log(msg: str) -> None:
    ts   = datetime.now().isoformat(timespec="seconds")
    line = f"{ts} {msg}"
    print(line)
    with open(LOG_FILE, "a") as f:
        f.write(line + "\n")


def _parse_events(html_content: str, heute: str) -> list[dict]:
    events     = []
    events_raw = re.findall(
        r'<div class="event mb-3"[^>]*>(.*?)(?=<div class="event mb-3"|$)',
        html_content, re.S)

    for e in events_raw:
        m_tag   = re.search(r'class="date-number"[^>]*>(\d+)<', e)
        m_monat = re.search(r'class="date-month"[^>]*>(\w+)<',  e)
        m_jahr  = re.search(r'class="date-year"[^>]*>(\d{4})<', e)
        if not (m_tag and m_monat and m_jahr):
            continue
        monat_nr = MONATE.get(m_monat.group(1), 0)
        if not monat_nr:
            continue
        datum = f'{m_jahr.group(1)}-{monat_nr:02d}-{int(m_tag.group(1)):02d}'
        if datum < heute:
            continue

        m_uhr   = re.search(r'(\d{2}:\d{2}) Uhr', e)
        uhrzeit = m_uhr.group(1) if m_uhr else ""

        texts = []
        for t in re.findall(r'>([^<>\n]{3,200})<', e):
            t = htmlmod.unescape(t.strip())
            if (not t or t in SKIP_TEXT or t in WOCHENTAGE
                    or t == m_monat.group(1)
                    or re.match(r'^\d{1,4}$', t)
                    or "Uhr" in t):
                continue
            texts.append(t)

        filtered    = [t for t in texts if t not in KATEGORIEN]
        verein      = filtered[0] if filtered else ""
        bezeichnung = filtered[1] if len(filtered) > 1 else ""
        ort         = filtered[2] if len(filtered) > 2 else ""

        if bezeichnung:
            events.append({"datum": datum, "uhrzeit": uhrzeit,
                           "bezeichnung": bezeichnung, "ort": ort,
                           "_verein_name": verein})
    return events


def _fetch(c_id: str) -> str | None:
    try:
        with urllib.request.urlopen(f"{API_BASE}?c={c_id}", timeout=15) as r:
            return r.read().decode("utf-8")
    except Exception as e:
        _log(f"  ❌ Fetch-Fehler c={c_id}: {e}")
        return None


def discover_c_id(url: str) -> str | None:
    """Nutzt Playwright einmalig um die c= Gemeinde-ID zu ermitteln."""
    from playwright.sync_api import sync_playwright
    found = []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page    = browser.new_page(viewport={"width": 1280, "height": 900})

        def on_request(req):
            if "heimat-info.de/embeddings" in req.url:
                m = re.search(r'c=([a-f0-9\-]{36})', req.url)
                if m:
                    found.append(m.group(1))
        page.on("request", on_request)

        try:
            page.goto(url, wait_until="networkidle", timeout=30000)
            page.evaluate("""() => {
                const btn = document.querySelector(".brlbs-cmpnt-cb-btn");
                if (btn) btn.click();
            }""")
            page.wait_for_timeout(8000)
        except Exception as e:
            _log(f"  Playwright-Fehler: {e}")
        finally:
            browser.close()
    return found[0] if found else None


def _existing_events() -> set[tuple[str, str]]:
    """Gibt alle (datum, bezeichnung)-Paare aus vereinstermine.json zurück (alle Keys)."""
    if not VEREINSTERMINE_FILE.exists():
        return set()
    data = json.loads(VEREINSTERMINE_FILE.read_text())
    existing = set()
    for key, items in data.items():
        if not isinstance(items, list):
            continue
        for item in items:
            if isinstance(item, dict) and "datum" in item:
                existing.add((item["datum"], item.get("bezeichnung", "").strip().lower()))
    return existing


def do_import(uid: str) -> str:
    """Schreibt bestätigte Events in vereinstermine.json – überspringt alle keys-übergreifenden Duplikate."""
    pending_file = Path(f"/tmp/heimat_pending_{uid}.json")
    if not pending_file.exists():
        return "⚠️ Pending-Datei nicht gefunden (Server-Neustart?)"

    pending  = json.loads(pending_file.read_text())
    events   = pending["events"]
    data     = json.loads(VEREINSTERMINE_FILE.read_text()) if VEREINSTERMINE_FILE.exists() else {}
    if "_labels" not in data:
        data["_labels"] = {}

    existing = _existing_events()
    neu = duplikat = 0

    for e in events:
        check = (e["datum"], e["bezeichnung"].strip().lower())
        if check in existing:
            duplikat += 1
            continue
        key = e["_verein_key"]
        if key not in data:
            data[key] = []
        if key not in data["_labels"]:
            data["_labels"][key] = e["_label"]
        data[key].append({
            "datum":       e["datum"],
            "uhrzeit":     e["uhrzeit"],
            "bezeichnung": e["bezeichnung"],
            "ort":         e["ort"],
            "ortschaft":   e.get("ortschaft", ""),
        })
        existing.add(check)
        neu += 1

    VEREINSTERMINE_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2))
    pending_file.unlink(missing_ok=True)
    _log(f"✅ Import: {neu} neu, {duplikat} Duplikate übersprungen")
    return f"✅ {neu} neue Termine importiert, {duplikat} Duplikate übersprungen"


def cmd_import(secrets: dict) -> None:
    token   = secrets["TOKEN"]
    chat_id = secrets["CHAT_ID"]

    if not GEMEINDEN_FILE.exists():
        send_telegram(token, chat_id, "⚠️ heimat_gemeinden.json nicht gefunden.\n"
                      "Gemeinde hinzufügen: /heimat-add <url>")
        return

    gemeinden = json.loads(GEMEINDEN_FILE.read_text())
    if not gemeinden:
        send_telegram(token, chat_id,
                      "ℹ️ Keine Gemeinden konfiguriert.\n/heimat-add <url> nutzen.")
        return

    heute    = datetime.now().strftime("%Y-%m-%d")
    existing = _existing_events()

    alle_events = []
    fehler      = []

    for g in gemeinden:
        _log(f"Fetche {g['name']} (c={g['c_id'][:8]}…)")
        html = _fetch(g["c_id"])
        if not html:
            fehler.append(g["name"])
            continue
        events = _parse_events(html, heute)
        for e in events:
            e["_verein_key"] = g["verein_key"]
            e["_label"]      = g.get("label", g["name"])
            e["_gemeinde"]   = g["name"]
            e["_neu"]        = (e["datum"], e["bezeichnung"].strip().lower()) not in existing
        alle_events.extend(events)
        neu_count = sum(1 for e in events if e["_neu"])
        _log(f"  → {len(events)} Termine ({neu_count} neu)")

    if not alle_events:
        send_telegram(token, chat_id, "🏡 heimat-info: Keine bevorstehenden Termine.")
        return

    alle_events.sort(key=lambda x: (x["datum"], x.get("uhrzeit", "")))
    neu_gesamt  = sum(1 for e in alle_events if e["_neu"])
    dup_gesamt  = len(alle_events) - neu_gesamt

    uid = str(uuid.uuid4())[:8]
    Path(f"/tmp/heimat_pending_{uid}.json").write_text(
        json.dumps({"uid": uid, "events": alle_events}, ensure_ascii=False))

    # Vorschau: nur neue Termine anzeigen, Duplikate zusammenfassen
    neue   = [e for e in alle_events if e["_neu"]]
    vorschau = "\n".join(
        f"• {e['datum']} {e.get('uhrzeit',''):5} – {e['bezeichnung'][:35]} [{e['_gemeinde']}]"
        for e in neue[:15])
    if len(neue) > 15:
        vorschau += f"\n… +{len(neue)-15} weitere neue"

    gemeinden_str = ", ".join(g["name"] for g in gemeinden)
    msg = (f"🏡 heimat-info Import\n"
           f"Gemeinden: {gemeinden_str}\n"
           f"Gesamt: {len(alle_events)} | 🆕 Neu: {neu_gesamt} | ⏭ Duplikate: {dup_gesamt}\n\n"
           + (vorschau if neue else "Alle Termine bereits vorhanden."))

    send_telegram_inline(token, chat_id, msg, [[
        {"text": f"✅ {neu_gesamt} importieren", "callback_data": f"heimat_ok:{uid}"},
        {"text": "❌ Verwerfen",                 "callback_data": f"heimat_no:{uid}"},
    ]])

    if fehler:
        send_telegram(token, chat_id, f"⚠️ Fetch-Fehler bei: {', '.join(fehler)}")


def cmd_add(url: str, secrets: dict) -> None:
    token   = secrets["TOKEN"]
    chat_id = secrets["CHAT_ID"]

    _log(f"Discovery: {url}")
    c_id = discover_c_id(url)

    if not c_id:
        send_telegram(token, chat_id,
                      f"❌ Keine heimat-info ID gefunden auf:\n{url}\n\n"
                      "Prüfe ob die Seite heimat-info nutzt und ein 'Inhalte entsperren'-Button vorhanden ist.")
        return

    # Slug aus URL ableiten
    slug = re.sub(r'https?://(www\.)?', '', url).split('/')[0].replace('gemeinde-', '').replace('.de', '').replace('.', '_')
    slug = re.sub(r'[^a-z0-9_]', '', slug.lower())[:20]
    name = slug.replace('_', ' ').title()

    gemeinden = json.loads(GEMEINDEN_FILE.read_text()) if GEMEINDEN_FILE.exists() else []

    # Duplikat prüfen
    if any(g["c_id"] == c_id for g in gemeinden):
        send_telegram(token, chat_id, f"ℹ️ Diese Gemeinde ist bereits eingetragen (c={c_id[:8]}…)")
        return

    eintrag = {"name": name, "label": name, "verein_key": slug,
                "c_id": c_id, "url": url}
    gemeinden.append(eintrag)
    GEMEINDEN_FILE.write_text(json.dumps(gemeinden, ensure_ascii=False, indent=2))

    send_telegram(token, chat_id,
                  f"✅ Gemeinde hinzugefügt:\n"
                  f"Name: {name}\nKey: {slug}\nID: {c_id[:8]}…\n\n"
                  f"Mit /heimat den ersten Import starten.")
    _log(f"✅ Gemeinde hinzugefügt: {name} ({c_id})")


def main() -> None:
    secrets = load_secrets()
    if len(sys.argv) >= 3 and sys.argv[1] == "--add":
        cmd_add(sys.argv[2], secrets)
    else:
        cmd_import(secrets)


if __name__ == "__main__":
    main()
