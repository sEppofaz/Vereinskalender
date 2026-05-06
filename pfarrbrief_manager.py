#!/opt/rename-webhook/bin/python3
"""
pfarrbrief_manager.py
Verarbeitet einen Pfarrbrief-Scan aus Dropbox:
- Extrahiert Gottesdienste via Claude Vision
- Filtert auf Hölskofen und Paindlkofen
- Speichert Termine in gottesdienste.json
- Verschiebt Pfarrbrief nach /Dokumente/Pfarrbriefe/
"""

import json
import re
import sys
import tempfile
import urllib.request
from datetime import datetime
from pathlib import Path

import anthropic
import dropbox

sys.path.insert(0, "/opt/rename-webhook")
from shared.secrets import load_secrets
from shared.telegram import send_telegram

GOTTESDIENSTE_FILE = Path("/opt/rename-webhook/gottesdienste.json")
def _normalize(s: str) -> str:
    return s.lower().replace("ö","oe").replace("ü","ue").replace("ä","ae").replace("ß","ss").strip()

def _ort_match(ort: str, kandidaten: list[str], schwelle: float = 0.75) -> bool:
    from difflib import SequenceMatcher
    n = _normalize(ort)
    for k in kandidaten:
        nk = _normalize(k)
        if nk in n or n in nk:
            return True
        ratio = SequenceMatcher(None, n, nk).ratio()
        if ratio >= schwelle:
            return True
    return False

ORTE_HK = ["hölskofen", "hoelskofen", "hölskofen"]
ORTE_PK = ["paindlkofen"]
ORTE_OK = ["oberköllnbach", "oberkoellnbach", "oberkollnbach"]
DROPBOX_ZIELORDNER = "/Dokumente/Pfarrbriefe"


def get_dropbox_client(secrets: dict) -> dropbox.Dropbox:
    return dropbox.Dropbox(
        oauth2_refresh_token=secrets["DROPBOX_REFRESH_TOKEN"],
        app_key=secrets["DROPBOX_APP_KEY"],
        app_secret=secrets["DROPBOX_APP_SECRET"],
    )


def download_file(dbx: dropbox.Dropbox, dropbox_path: str) -> bytes:
    _, response = dbx.files_download(dropbox_path)
    return response.content


def extract_gottesdienste(api_key: str, file_bytes: bytes, filename: str) -> list[dict]:
    import base64 as b64mod
    ext        = Path(filename).suffix.lower()
    media_type = "application/pdf" if ext == ".pdf" else "image/jpeg"
    block_type = "document" if ext == ".pdf" else "image"
    data_b64   = b64mod.standard_b64encode(file_bytes).decode("utf-8")

    prompt = """Lies dieses Dokument vollständig durch alle Seiten.
Extrahiere ALLE Gottesdienst-Termine. Achte besonders auf Ortsangaben wie Hölskofen und Paindlkofen.
Gib das Ergebnis als JSON-Array zurück:
[{"datum":"YYYY-MM-DD","uhrzeit":"HH:MM","ort":"Ortsname","art":"Art des Gottesdienstes"}]
Nur das JSON-Array, nichts anderes. Wenn kein Termin gefunden: []."""

    payload = json.dumps({
        "model": "claude-sonnet-4-6",
        "max_tokens": 4096,
        "messages": [{"role": "user", "content": [
            {"type": block_type, "source": {"type": "base64", "media_type": media_type, "data": data_b64}},
            {"type": "text", "text": prompt},
        ]}],
    }).encode()

    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=payload,
        headers={
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=60) as r:
        result = json.loads(r.read())

    text = result["content"][0]["text"].strip()
    text = re.sub(r"```json|```", "", text).strip()
    s, e = text.find("["), text.rfind("]")
    if s == -1 or e == -1:
        return []
    return json.loads(text[s:e+1])


def filter_hk(termine: list[dict]) -> list[dict]:
    return [t for t in termine if _ort_match(t.get("ort", ""), ORTE_HK)]

def filter_pk(termine: list[dict]) -> list[dict]:
    return [t for t in termine if _ort_match(t.get("ort", ""), ORTE_PK)]

def filter_ok(termine: list[dict]) -> list[dict]:
    return [t for t in termine if _ort_match(t.get("ort", ""), ORTE_OK)]


def load_gottesdienste() -> dict:
    if GOTTESDIENSTE_FILE.exists():
        try:
            data = json.loads(GOTTESDIENSTE_FILE.read_text())
            if isinstance(data, list):
                return {"hk": data, "pk": [], "ok": []}
            # Migration hk_pk → hk+pk
            if "hk_pk" in data:
                return {"hk": data.get("hk_pk", []), "pk": [], "ok": data.get("ok", [])}
            return data
        except Exception:
            pass
    return {"hk": [], "pk": [], "ok": []}


def save_gottesdienste(data: dict) -> None:
    GOTTESDIENSTE_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2))


def merge_termine(bestehende: list, neue: list) -> list:
    keys = {(t["datum"], t["uhrzeit"], t["ort"]) for t in bestehende}
    for t in neue:
        k = (t["datum"], t["uhrzeit"], t["ort"])
        if k not in keys:
            bestehende.append(t)
            keys.add(k)
    heute = datetime.now().strftime("%Y-%m-%d")
    bestehende = [t for t in bestehende if t["datum"] >= heute]
    bestehende.sort(key=lambda t: (t["datum"], t["uhrzeit"]))
    return bestehende


def main():
    if len(sys.argv) < 2:
        print("Usage: pfarrbrief_manager.py <dropbox_path>")
        sys.exit(1)

    dropbox_path = sys.argv[1]
    filename     = Path(dropbox_path).name

    secrets  = load_secrets()
    dbx      = get_dropbox_client(secrets)
    api_key  = secrets["CLAUDE_API_KEY"]
    tg_token = secrets["TOKEN"]
    chat_id  = secrets["CHAT_ID"]

    print(f"📋 Verarbeite Pfarrbrief: {filename}")

    # Datei herunterladen
    file_bytes = download_file(dbx, dropbox_path)

    # Termine extrahieren
    alle_termine = extract_gottesdienste(api_key, file_bytes, filename)
    print(f"   {len(alle_termine)} Termine gefunden")

    # Filtern
    hk = filter_hk(alle_termine)
    pk = filter_pk(alle_termine)
    ok = filter_ok(alle_termine)
    print(f"   {len(hk)} Termine Hölskofen, {len(pk)} Paindlkofen, {len(ok)} Oberköllnbach")

    # Gottesdienste.json aktualisieren
    data = load_gottesdienste()
    data["hk"] = merge_termine(data["hk"], hk)
    data["pk"] = merge_termine(data["pk"], pk)
    data["ok"] = merge_termine(data["ok"], ok)
    save_gottesdienste(data)

    # Pfarrbrief in Zielordner verschieben (Dateiname vom Rename-Job bereits korrekt)
    ziel_path = f"{DROPBOX_ZIELORDNER}/{filename}"
    try:
        dbx.files_move_v2(dropbox_path, ziel_path, autorename=True)
        print(f"   Verschoben nach {ziel_path}")
    except Exception as e:
        print(f"   ⚠️ Verschieben fehlgeschlagen: {e}")

    # Telegram-Bestätigung
    zeilen = [f"📋 Pfarrbrief verarbeitet: {filename}\n"]
    if hk or pk:
        for t in sorted(hk + pk, key=lambda x: (x["datum"], x["uhrzeit"])):
            datum = datetime.strptime(t["datum"], "%Y-%m-%d").strftime("%d.%m.%Y")
            zeilen.append(f"• {datum} {t['uhrzeit']} Uhr – {t['ort']}: {t['art']}")
    else:
        zeilen.append("ℹ️ Keine Termine in Hölskofen/Paindlkofen.")
    zeilen.append(f"\n📍 {len(ok)} Termine Oberköllnbach gespeichert (/Pfarrbrief-ok)")

    send_telegram(tg_token, chat_id, "\n".join(zeilen))
    print("✅ Fertig")


if __name__ == "__main__":
    main()
