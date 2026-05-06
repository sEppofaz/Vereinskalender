#!/opt/rename-webhook/bin/python3
"""
verein_manager.py
Single-Verein-Modus (ff/kp): Termine speichern + Telegram senden.
Multi-Verein-Modus (Gemeinde-Kalender): Termine pro Verein gruppieren, kein Telegram.
"""

import json
import re
import sys
import urllib.request
from datetime import datetime
from pathlib import Path

import dropbox

sys.path.insert(0, "/opt/rename-webhook")
from shared.kalender_store import KalenderStore
from shared.secrets import load_secrets
from shared.telegram import send_telegram


VEREINSTERMINE_FILE = Path("/opt/rename-webhook/vereinstermine.json")
DROPBOX_ZIELORDNER  = "/Dokumente/sEpp/Vereine"


def get_dropbox_client(secrets: dict) -> dropbox.Dropbox:
    return dropbox.Dropbox(
        oauth2_refresh_token=secrets["DROPBOX_REFRESH_TOKEN"],
        app_key=secrets["DROPBOX_APP_KEY"],
        app_secret=secrets["DROPBOX_APP_SECRET"],
    )


def make_key(verein_name: str) -> str:
    """FFW Postau → ffw_postau"""
    name = verein_name.lower()
    for a, b in [("ä","ae"),("ö","oe"),("ü","ue"),("ß","ss")]:
        name = name.replace(a, b)
    name = re.sub(r"[^a-z0-9 ]", "", name)
    name = re.sub(r"\s+", "_", name.strip())
    return name[:30] or "verein"


def is_multi_club(filename: str) -> bool:
    """Mehrvereins-Kalender wenn 'gemeinde' oder 'veranstaltungskalender' im Namen,
    aber kein konkreter Vereinsname (ff, kp, feuerwehr, patrioten)."""
    name = filename.lower()
    has_specific = any(x in name for x in ["ff_", "kp_", "feuerwehr", "patrioten"])
    has_multi    = any(x in name for x in ["gemeinde", "veranstaltungskalender"])
    return has_multi and not has_specific


def detect_verein_key(filename: str) -> str:
    name = filename.lower()
    if "ff_" in name or "feuerwehr" in name:
        return "ff"
    if "kp_" in name or "patrioten" in name or "koenigtreue" in name or "königstreue" in name:
        return "kp"
    return "ff"


def _call_claude(api_key: str, file_bytes: bytes, filename: str, prompt: str) -> str:
    import base64 as b64
    ext        = Path(filename).suffix.lower()
    media_type = "application/pdf" if ext == ".pdf" else "image/jpeg"
    block_type = "document" if ext == ".pdf" else "image"
    payload    = json.dumps({
        "model": "claude-sonnet-4-6",
        "max_tokens": 4096,
        "messages": [{"role": "user", "content": [
            {"type": block_type, "source": {"type": "base64", "media_type": media_type,
                                            "data": b64.standard_b64encode(file_bytes).decode()}},
            {"type": "text", "text": prompt},
        ]}],
    }).encode()
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages", data=payload,
        headers={"x-api-key": api_key, "anthropic-version": "2023-06-01",
                 "content-type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read())["content"][0]["text"].strip()


def _parse_array(text: str) -> list:
    text = re.sub(r"```json|```", "", text).strip()
    s, e = text.find("["), text.rfind("]")
    return json.loads(text[s:e+1]) if s != -1 and e != -1 else []


def extract_termine_single(api_key: str, file_bytes: bytes, filename: str) -> list:
    prompt = (
        "Lies dieses Dokument vollständig durch alle Seiten.\n"
        "Extrahiere ALLE Termine und Veranstaltungen des Vereins.\n"
        "Gib das Ergebnis als JSON-Array zurück:\n"
        '[{"datum":"YYYY-MM-DD","uhrzeit":"HH:MM","ort":"Ortsname","bezeichnung":"Name der Veranstaltung"}]\n'
        "Regeln:\n"
        "- uhrzeit: nur wenn explizit angegeben, sonst \"\"\n"
        "- ort: nur wenn explizit angegeben, sonst \"\"\n"
        "- Wenn Zeitraum (z.B. \"14.-16. März\"): einen Eintrag pro Tag\n"
        "- Nur das JSON-Array, nichts anderes. Wenn keine Termine: []"
    )
    return _parse_array(_call_claude(api_key, file_bytes, filename, prompt))


def extract_termine_multi(api_key: str, file_bytes: bytes, filename: str) -> list:
    prompt = (
        "Lies diesen Veranstaltungskalender vollständig durch alle Seiten.\n"
        "Er enthält Termine mehrerer Vereine oder Gruppen der Gemeinde.\n"
        "Extrahiere ALLE Termine und ordne jeden dem richtigen Verein zu.\n"
        "Gib das Ergebnis als JSON-Array zurück:\n"
        '[{"verein":"Vollständiger Vereinsname","datum":"YYYY-MM-DD","uhrzeit":"HH:MM","ort":"Ortsname","bezeichnung":"Veranstaltung"}]\n'
        "Regeln:\n"
        "- verein: exakter Name des Vereins wie im Dokument angegeben\n"
        "- uhrzeit: nur wenn explizit angegeben, sonst \"\"\n"
        "- ort: nur wenn explizit angegeben, sonst \"\"\n"
        "- Wenn Zeitraum (z.B. \"14.-16. März\"): einen Eintrag pro Tag\n"
        "- Nur das JSON-Array, nichts anderes. Wenn keine Termine: []"
    )
    return _parse_array(_call_claude(api_key, file_bytes, filename, prompt))


def load_vereinstermine() -> dict:
    if VEREINSTERMINE_FILE.exists():
        try:
            return json.loads(VEREINSTERMINE_FILE.read_text())
        except Exception:
            pass
    return {"_labels": {"ff": "FF Hölskofen", "kp": "Königstreue Patrioten Hölskofen"}, "ff": [], "kp": []}


def save_vereinstermine(data: dict) -> None:
    KalenderStore.update(lambda d: d.clear() or d.update(data))


def merge_termine(bestehende: list, neue: list) -> list:
    ex_bez = {(t["datum"], t.get("bezeichnung", "")) for t in bestehende}
    ex_dzo = {(t["datum"], t.get("uhrzeit", ""), t.get("ort", ""))
              for t in bestehende if t.get("uhrzeit") or t.get("ort")}
    for t in neue:
        k_bez = (t["datum"], t.get("bezeichnung", ""))
        k_dzo = (t["datum"], t.get("uhrzeit", ""), t.get("ort", ""))
        is_dup = k_bez in ex_bez or (
            (t.get("uhrzeit") or t.get("ort")) and k_dzo in ex_dzo
        )
        if not is_dup:
            bestehende.append(t)
            ex_bez.add(k_bez)
            if t.get("uhrzeit") or t.get("ort"):
                ex_dzo.add(k_dzo)
    heute  = datetime.now().strftime("%Y-%m-%d")
    result = [t for t in bestehende if t["datum"] >= heute]
    result.sort(key=lambda t: (t["datum"], t.get("uhrzeit", "")))
    return result


def main():
    if len(sys.argv) < 2:
        print("Usage: verein_manager.py <dropbox_path>")
        sys.exit(1)

    dropbox_path = sys.argv[1]
    filename     = Path(dropbox_path).name

    secrets = load_secrets()
    dbx     = get_dropbox_client(secrets)
    api_key = secrets["CLAUDE_API_KEY"]

    print(f"📅 Verarbeite Kalender: {filename}")
    _, response = dbx.files_download(dropbox_path)
    file_bytes  = response.content

    data   = load_vereinstermine()
    labels = data.get("_labels", {"ff": "FF Hölskofen", "kp": "Königstreue Patrioten"})

    if is_multi_club(filename):
        # ── Mehrvereins-Modus ────────────────────────────────────────────────
        alle = extract_termine_multi(api_key, file_bytes, filename)
        print(f"   {len(alle)} Termine extrahiert (Multi-Verein-Modus)")

        by_verein: dict = {}
        for t in alle:
            name = t.pop("verein", "Unbekannt").strip()
            by_verein.setdefault(name, []).append(t)

        for verein_name, termine in by_verein.items():
            key       = make_key(verein_name)
            labels[key] = verein_name
            data[key]   = merge_termine(data.get(key, []), termine)
            print(f"   {verein_name} → {key}: {len(data[key])} Termine")

        data["_labels"] = labels
        save_vereinstermine(data)
        print(f"✅ {sum(len(v) for k,v in data.items() if not k.startswith('_') and isinstance(v,list))} Termine gespeichert")

    else:
        # ── Einzelverein-Modus (ff / kp) ─────────────────────────────────────
        verein_key   = detect_verein_key(filename)
        verein_label = labels.get(verein_key, verein_key.upper())

        alle = extract_termine_single(api_key, file_bytes, filename)
        print(f"   {len(alle)} Termine gefunden ({verein_label})")

        data[verein_key] = merge_termine(data.get(verein_key, []), alle)
        data["_labels"]  = labels
        save_vereinstermine(data)
        print(f"   {len(data[verein_key])} Termine gespeichert")

        send_telegram(
            secrets["TOKEN"], secrets["CHAT_ID"],
            f"📅 Jahreskalender verarbeitet: {filename}\n"
            f"Verein: {verein_label}\n"
            f"{len(alle)} Termine gefunden, {len(data[verein_key])} gespeichert.",
        )

    # Datei in Vereine-Ordner verschieben
    ziel = f"{DROPBOX_ZIELORDNER}/{filename}"
    try:
        dbx.files_move_v2(dropbox_path, ziel, autorename=True)
        print(f"   Verschoben nach {ziel}")
    except Exception as e:
        print(f"   ⚠️ Verschieben fehlgeschlagen: {e}")

    print("✅ Fertig")


if __name__ == "__main__":
    main()
