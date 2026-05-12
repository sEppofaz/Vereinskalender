import base64 as b64
import io as _io
import json
import os
import re
import time
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path

from shared.kalender_store import KalenderStore

try:
    from pillow_heif import register_heif_opener
    register_heif_opener()
    _HEIC_SUPPORTED = True
except ImportError:
    _HEIC_SUPPORTED = False

from PIL import Image

CLAUDE_API_KEY = os.environ["CLAUDE_API_KEY"]

VEREINSTERMINE_FILE = Path("/opt/rename-webhook/vereinstermine.json")
GOTTESDIENSTE_FILE  = Path("/opt/rename-webhook/gottesdienste.json")
KALENDER_HTML_FILE  = Path("/opt/rename-webhook/kalender.html")
ICON_192_FILE       = Path("/opt/rename-webhook/icon-192.png")
ICON_512_FILE       = Path("/opt/rename-webhook/icon-512.png")

MEDIA_TYPES = {
    ".pdf":  "application/pdf",
    ".jpg":  "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png":  "image/png",
    ".tiff": "image/tiff",
    ".tif":  "image/tiff",
    ".webp": "image/webp",
    ".bmp":  "image/bmp",
}

_PG_KEYS   = {"hk": "pfarrgemeinde", "pk": "pfarrgemeinde", "ok": "pfarrgemeinde"}
_PG_LABELS = {"pfarrgemeinde": "Pfarrgemeinde Postau"}

# Shared in-memory store: uid → pending kalender import (rename writes, telegram reads)
_kalender_pending: dict = {}


def log(msg: str) -> None:
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


def lookup_plz(plz: str) -> dict:
    """PLZ → Gemeinde + Landkreis via Nominatim."""
    url = "https://nominatim.openstreetmap.org/search?" + urllib.parse.urlencode({
        "postalcode": plz, "country": "de", "format": "json",
        "limit": "1", "addressdetails": "1",
    })
    time.sleep(1)
    req = urllib.request.Request(url, headers={"User-Agent": "VereinskalenderApp/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            results = json.loads(r.read())
        if results:
            addr      = results[0].get("address", {})
            gemeinde  = (addr.get("city") or addr.get("town") or
                         addr.get("village") or addr.get("municipality") or "")
            landkreis = (addr.get("county") or addr.get("state_district") or "")
            landkreis = re.sub(r"^(Landkreis|Kreis)\s+", "", landkreis).strip()
            log(f"📍  PLZ {plz} → {gemeinde}, {landkreis}")
            return {"plz": plz, "gemeinde": gemeinde, "landkreis": landkreis}
    except Exception as e:
        log(f"⚠️  PLZ-Lookup ({plz}): {e}")
    return {"plz": plz, "gemeinde": "", "landkreis": ""}


def _make_verein_key(verein_name: str) -> str:
    key = verein_name.lower()
    for a, b in [("ä", "ae"), ("ö", "oe"), ("ü", "ue"), ("ß", "ss")]:
        key = key.replace(a, b)
    key = re.sub(r"[^a-z0-9 ]", "", key)
    return re.sub(r"\s+", "_", key.strip())[:30] or "verein"


def _levenshtein(a: str, b: str) -> int:
    if len(a) < len(b):
        a, b = b, a
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        curr = [i]
        for j, cb in enumerate(b, 1):
            curr.append(min(prev[j] + 1, curr[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = curr
    return prev[-1]


def find_similar_keys(new_key: str, existing_labels: dict) -> list:
    """Gibt bestehende Keys zurück die dem new_key ähneln (Duplikat-Verdacht)."""
    result = []
    for key, name in existing_labels.items():
        if key == new_key:
            continue
        shorter, longer = (new_key, key) if len(new_key) <= len(key) else (key, new_key)
        is_prefix = longer.startswith(shorter + "_") or longer.startswith(shorter + " ")
        if is_prefix:
            result.append({"key": key, "name": name})
            continue
        if len(shorter) >= 4:
            threshold = max(1, len(shorter) // 5)
            if _levenshtein(new_key, key) <= threshold:
                result.append({"key": key, "name": name})
    return result


def import_pdf_bytes(file_bytes: bytes, suffix: str) -> dict:
    """Ruft Claude Vision auf, gibt {"alle": [...], "auto_plz": "..."} zurück."""
    if suffix in {".heic", ".heif"}:
        with Image.open(_io.BytesIO(file_bytes)) as img:
            buf = _io.BytesIO()
            img.convert("RGB").save(buf, format="JPEG", quality=92)
            file_bytes = buf.getvalue()
        suffix = ".jpg"
        log("🔄  HEIC → JPEG konvertiert")

    is_pdf = suffix == ".pdf"
    intro = (
        "Lies diesen Veranstaltungskalender vollständig durch ALLE Seiten – "
        "keine Ortschaft, keine Gruppe, keinen Abschnitt überspringen.\n"
        if is_pdf else
        "Lies dieses Foto/diesen Scan eines Veranstaltungskalenders – "
        "extrahiere alle sichtbaren Termine.\n"
    )
    prompt = (
        intro +
        "Das Dokument kann Termine von Vereinen, Pfarreien, Gemeinden oder anderen Gruppen enthalten.\n"
        "Extrahiere JEDEN Termin aus JEDER Ortschaft und ordne ihn dem richtigen Verein/der richtigen Gruppe zu.\n\n"
        "Gib genau dieses JSON-Objekt zurück, nichts anderes:\n"
        '{"plz":"NNNNN","termine":[{"verein":"Name","datum":"YYYY-MM-DD","uhrzeit":"HH:MM",'
        '"ort":"GH Hölskofen","ortschaft":"Hölskofen","bezeichnung":"Veranstaltung"}]}\n\n'
        "Regeln:\n"
        "- plz: Postleitzahl des Hauptorts aus dem Dokument, oder \"\" wenn nicht erkennbar\n"
        "- verein: Exakter Name des Vereins/der Gruppe wie im Dokument.\n"
        "  Kein Verein benannt, Pfarrbrief/Pfarrkalender: 'Pfarrgemeinde [Ortsname]'\n"
        "  Kein Verein benannt, Gemeindeblatt/Gemeindekalender: 'Gemeinde [Ortsname]'\n"
        "- datum: YYYY-MM-DD\n"
        "- uhrzeit: HH:MM nur wenn explizit angegeben, sonst \"\"\n"
        "- ort: Veranstaltungsort wie im Dokument angegeben (Gebäude + Ortsname), "
        "z.B. \"GH Hölskofen\", \"Pfarrheim Postau\"\n"
        "- ortschaft: NUR der Gemeinde-/Dorfname des Veranstaltungsorts, "
        "z.B. \"Hölskofen\", \"Postau\", \"Paindlkofen\". Leer wenn nicht erkennbar.\n"
        "- Mehrtägige Termine (z.B. '14.–16. März'): IMMER einen separaten Eintrag pro Tag\n"
        "- Termin an mehreren Ortschaften (z.B. 'Hölskofen und Postau'): IMMER einen separaten "
        "Eintrag pro Ortschaft – gleiche Bezeichnung, unterschiedlicher ort\n"
        "- Termin mehrerer Vereine zusammen (z.B. 'FF und KLJB gemeinsam'): IMMER einen separaten "
        "Eintrag pro Verein – gleiche Bezeichnung und ort, unterschiedlicher verein\n"
        "- Wenn keine Termine: {\"plz\":\"\",\"termine\":[]}"
    )

    encoded = b64.standard_b64encode(file_bytes).decode()
    if is_pdf:
        claude_content = [
            {"type": "document", "source": {"type": "base64",
             "media_type": "application/pdf", "data": encoded}},
            {"type": "text", "text": prompt},
        ]
    else:
        media_type = MEDIA_TYPES.get(suffix, "image/jpeg")
        claude_content = [
            {"type": "image", "source": {"type": "base64",
             "media_type": media_type, "data": encoded}},
            {"type": "text", "text": prompt},
        ]

    payload = json.dumps({
        "model": "claude-sonnet-4-6",
        "max_tokens": 8192,
        "messages": [{"role": "user", "content": claude_content}],
    }).encode()
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages", data=payload,
        headers={
            "x-api-key": CLAUDE_API_KEY,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=120) as r:
        raw_text = json.loads(r.read())["content"][0]["text"].strip()

    raw_text = re.sub(r"```json|```", "", raw_text).strip()

    def _parse_claude_json(text):
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            pass
        s, e = text.find("{"), text.rfind("}")
        chunk = text[s:e + 1] if s != -1 else text
        try:
            return json.loads(chunk)
        except json.JSONDecodeError:
            pass
        fixed = re.sub(r",(\s*[}\]])", r"\1", chunk)
        try:
            return json.loads(fixed)
        except json.JSONDecodeError:
            pass
        plz_m = re.search(r'"plz"\s*:\s*"(\d{5})"', text)
        termine = []
        for m in re.finditer(r'\{[^{}]*"datum"\s*:\s*"\d{4}-\d{2}-\d{2}"[^{}]*\}', text, re.DOTALL):
            try:
                termine.append(json.loads(m.group(0)))
            except Exception:
                def _f(n, t):
                    r = re.search(rf'"{n}"\s*:\s*"([^"]*)"', t)
                    return r.group(1) if r else ""
                termine.append({k: _f(k, m.group(0)) for k in ("verein", "datum", "uhrzeit", "ort", "bezeichnung")})
        log(f"⚠️  JSON-Fallback (Strategie 4): {len(termine)} Termine gerettet")
        return {"plz": plz_m.group(1) if plz_m else "", "termine": termine}

    parsed = _parse_claude_json(raw_text)
    if isinstance(parsed, list):
        return {"alle": parsed, "auto_plz": ""}
    return {"alle": parsed.get("termine", []), "auto_plz": parsed.get("plz", "")}


def _do_save_import(alle: list, auto_plz: str, form_plz: str, data: dict,
                    verein_ortschaften: dict | None = None,
                    key_remappings: dict | None = None) -> tuple:
    """Speichert importierte Termine in vereinstermine.json (data wird mutiert und gespeichert)."""
    labels = data.get("_labels", {"ff": "FF Hölskofen", "kp": "Königstreue Patrioten"})
    heute  = datetime.now().strftime("%Y-%m-%d")

    by_verein: dict = {}
    for t in alle:
        t_copy = {k: v for k, v in t.items() if k != "verein"}
        name = (t.get("verein") or "Unbekannt").strip() or "Unbekannt"
        by_verein.setdefault(name, []).append(t_copy)

    result_vereine = []
    for verein_name, termine in by_verein.items():
        key = _make_verein_key(verein_name)
        remapped = key_remappings and key in key_remappings
        if remapped:
            key = key_remappings[key]  # merge into existing verein
        else:
            labels[key] = verein_name
        bestehende = [t for t in data.get(key, []) if t.get("datum", "") >= heute]
        ex_bez = {(t["datum"], t.get("bezeichnung", "")) for t in bestehende}
        ex_dzo = {(t["datum"], t.get("uhrzeit", ""), t.get("ort", ""))
                  for t in bestehende if t.get("uhrzeit") or t.get("ort")}
        for t in termine:
            k_bez = (t.get("datum", ""), t.get("bezeichnung", ""))
            k_dzo = (t.get("datum", ""), t.get("uhrzeit", ""), t.get("ort", ""))
            is_dup = k_bez in ex_bez or (
                (t.get("uhrzeit") or t.get("ort")) and k_dzo in ex_dzo
            )
            if not is_dup:
                bestehende.append(t)
                ex_bez.add(k_bez)
                if t.get("uhrzeit") or t.get("ort"):
                    ex_dzo.add(k_dzo)
        bestehende = sorted(
            [t for t in bestehende if t.get("datum", "") >= heute],
            key=lambda t: (t["datum"], t.get("uhrzeit", ""))
        )
        data[key] = bestehende
        result_vereine.append({"name": verein_name, "key": key, "count": len(termine)})

    data["_labels"] = labels

    plz = form_plz
    if not plz and re.match(r"^\d{5}$", auto_plz):
        plz = auto_plz
        log(f"📮  PLZ automatisch erkannt: {plz}")
    data.setdefault("_meta", {})
    if plz and re.match(r"^\d{5}$", plz):
        meta_info = lookup_plz(plz)
        for v in result_vereine:
            existing = data["_meta"].get(v["key"], {})
            data["_meta"][v["key"]] = {
                **meta_info,
                **{k: w for k, w in existing.items() if k == "heimatort"},
            }

    if verein_ortschaften:
        for v in result_vereine:
            ort = (verein_ortschaften.get(v["key"]) or "").strip()
            if ort:
                data["_meta"].setdefault(v["key"], {})["heimatort"] = ort

    KalenderStore.update(lambda d: d.clear() or d.update(data))

    total = sum(v["count"] for v in result_vereine)
    log(f"📥  Import: {total} Termine, {len(result_vereine)} Vereine gespeichert")
    Path("/opt/rename-webhook/last_import.json").write_text(
        json.dumps({
            "datum":   datetime.now().strftime("%Y-%m-%d %H:%M"),
            "termine": total,
            "vereine": len(result_vereine),
        }, ensure_ascii=False)
    )
    return result_vereine, total


def parse_excel_bytes(file_bytes: bytes) -> list:
    """Parst eine .xlsx-Datei nach dem Vereinskalender-Template und gibt Termin-Dicts zurück."""
    import openpyxl
    wb = openpyxl.load_workbook(_io.BytesIO(file_bytes), data_only=True, read_only=True)
    ws = wb.active
    termine = []
    for row in ws.iter_rows(min_row=2, values_only=True):
        if not row or len(row) < 3:
            continue
        datum_raw, uhrzeit_raw, bezeichnung_raw = row[0], row[1], row[2]
        ort_raw       = row[3] if len(row) > 3 else None
        ortschaft_raw = row[4] if len(row) > 4 else None
        verein_raw    = row[5] if len(row) > 5 else None

        if not datum_raw or not bezeichnung_raw:
            continue

        bezeichnung = str(bezeichnung_raw).strip()
        if not bezeichnung:
            continue

        # Datum normalisieren
        if isinstance(datum_raw, datetime):
            datum_str = datum_raw.strftime("%Y-%m-%d")
        else:
            datum_str = str(datum_raw).strip()
            m = re.match(r"(\d{1,2})\.(\d{1,2})\.(\d{4})", datum_str)
            if m:
                datum_str = f"{m.group(3)}-{m.group(2).zfill(2)}-{m.group(1).zfill(2)}"
            if not re.match(r"^\d{4}-\d{2}-\d{2}$", datum_str):
                continue

        # Uhrzeit normalisieren
        uhrzeit_str = ""
        if uhrzeit_raw is not None and str(uhrzeit_raw).strip():
            if isinstance(uhrzeit_raw, datetime):
                uhrzeit_str = uhrzeit_raw.strftime("%H:%M")
            else:
                raw = str(uhrzeit_raw).strip()
                m = re.match(r"(\d{1,2}):(\d{2})", raw)
                if m:
                    uhrzeit_str = f"{m.group(1).zfill(2)}:{m.group(2)}"

        termine.append({
            "datum":      datum_str,
            "uhrzeit":    uhrzeit_str,
            "bezeichnung": bezeichnung,
            "ort":        str(ort_raw).strip() if ort_raw else "",
            "ortschaft":  str(ortschaft_raw).strip() if ortschaft_raw else "",
            "verein":     str(verein_raw).strip() if verein_raw else "",
        })
    wb.close()
    log(f"📊  Excel-Import: {len(termine)} Termine geparst")
    return termine
