import base64
import hashlib
import hmac
import json
import os
import re
import subprocess
import tempfile
import threading
import uuid as _uuid
from datetime import datetime
from pathlib import Path

import anthropic
import dropbox
from dropbox.files import DeletedMetadata, FolderMetadata
from flask import Blueprint, abort, request

from shared.flask_notify import TELEGRAM_CHAT_ID, send_telegram, send_telegram_inline
from shared.kalender_core import (
    MEDIA_TYPES,
    VEREINSTERMINE_FILE,
    _HEIC_SUPPORTED,
    _do_save_import,
    _kalender_pending,
    import_pdf_bytes,
    log,
)

rename_bp = Blueprint("rename", __name__)

DROPBOX_REFRESH_TOKEN = os.environ["DROPBOX_REFRESH_TOKEN"]
DROPBOX_APP_KEY       = os.environ["DROPBOX_APP_KEY"]
DROPBOX_APP_SECRET    = os.environ["DROPBOX_APP_SECRET"]
CLAUDE_API_KEY        = os.environ["CLAUDE_API_KEY"]
WATCH_FOLDER          = "/_gescannt-unsortiert"
CURSOR_FILE           = "/opt/rename-webhook/cursor.txt"
KALENDER_INPUT_FOLDER = "/Dokumente/Vereinskalender/input"
KALENDER_DONE_FOLDER  = "/Dokumente/Vereinskalender/verarbeitet"
KALENDER_CURSOR_FILE  = "/opt/rename-webhook/kalender_input_cursor.txt"
MODEL                 = "claude-haiku-4-5"

ALLOWED_EXTENSIONS = {
    ".pdf", ".jpg", ".jpeg", ".png", ".tiff", ".tif",
    ".heic", ".heif", ".webp", ".bmp",
    ".txt", ".rtf",
}


def get_dropbox_client() -> dropbox.Dropbox:
    return dropbox.Dropbox(
        oauth2_refresh_token=DROPBOX_REFRESH_TOKEN,
        app_key=DROPBOX_APP_KEY,
        app_secret=DROPBOX_APP_SECRET,
    )


def is_already_renamed(filename: str) -> bool:
    stem = Path(filename).stem
    return bool(
        re.match(r"^\d{4}[-_]\d{2}[-_]\d{2}_.+", stem) or
        re.match(r"^\d{4}_.+", stem)
    )


def get_cursor(dbx: dropbox.Dropbox) -> str:
    if Path(CURSOR_FILE).exists():
        return Path(CURSOR_FILE).read_text().strip()
    result = dbx.files_list_folder(WATCH_FOLDER, recursive=False)
    cursor = result.cursor
    while result.has_more:
        result = dbx.files_list_folder_continue(cursor)
        cursor = result.cursor
    Path(CURSOR_FILE).write_text(cursor)
    log("🔖  Neuer Cursor gespeichert")
    return cursor


def save_cursor(cursor: str) -> None:
    Path(CURSOR_FILE).write_text(cursor)


def rename_via_claude(dbx: dropbox.Dropbox, dropbox_path: str) -> None:
    filename = Path(dropbox_path).name
    suffix   = Path(dropbox_path).suffix.lower()

    if is_already_renamed(filename):
        return
    if suffix not in ALLOWED_EXTENSIONS:
        return

    log(f"📄  Verarbeite: {filename}")

    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
        tmp_path = tmp.name
    dbx.files_download_to_file(tmp_path, dropbox_path)

    try:
        data      = base64.standard_b64encode(Path(tmp_path).read_bytes()).decode("utf-8")
        today     = datetime.now().strftime("%Y-%m-%d")
        media_type = MEDIA_TYPES.get(suffix, "application/octet-stream")

        prompt = f"""Analysiere dieses Dokument und gib ausschließlich den neuen Dateinamen zurück (ohne Pfad, ohne Erklärung, ohne Anführungszeichen).

Namensschema: YYYY-MM-DD_Kategorie_Firma_Schlagwort_Betrag{suffix}

Regeln:
- Datum: Extrahiere das wirksame Datum aus dem Dokument (heute ist {today}).
  Falls nur Monat+Jahr vorhanden, nimm den 01. des Monats.
  Bei einem Kontoauszug: nimm das Datum vom ERSTEN "Kontostand am"-Eintrag im Dokument – das ist das Datum des Auszugsstands (z.B. "Kontostand am 30.12.2025, Auszug Nr. 12"). Ignoriere spätere Zeitstempel wie "Kontostand am 30.01.2026 um 20:05 Uhr", das ist nur der Druckzeitpunkt.
- Kategorie: z.B. Rechnung, Lieferschein, Abrechnung, Gebühren, Bescheid, Kontakt, Kontoauszug
- Firma: Name der Firma oder Behörde
- Schlagwort: kurze Beschreibung des Inhalts
- Betrag: Immer den Bruttobetrag (inkl. MwSt) verwenden. Vorzeichen aus Sicht des Empfängers (Josef Fischer):
  Ausgaben (er zahlt) → -70.00€ · Einnahmen/Erstattungen (er bekommt Geld) → +12.50€. Kein Betrag → weglassen.
  Wichtig: „Nachzahlung" = Ausgabe (−), „Erstattung" = Einnahme (+) – unabhängig davon, wie der Betrag
  intern im Dokument dargestellt ist (dort erscheint eine Erstattung oft als negativer Betrag).
- Kontoauszug – statt Betrag schreibe: Konto_<Kontonummer>
  Kontonummer ermitteln (zwei Wege, bevorzuge Weg 1):
  Weg 1: Die Kontonummer steht oft direkt im Dokument, z.B. "Giro Online 998087, DE90 ..." oder "Geldmarktkonto 4458532, DE27 ...".
          Nimm die Zahl, die direkt vor der IBAN steht.
  Weg 2: IBAN-Extraktion. Eine deutsche IBAN hat exakt 22 Zeichen (ohne Leerzeichen):
          Position 1-2:  Ländercode "DE"
          Position 3-4:  Prüfziffer (2 Stellen)
          Position 5-12: Bankleitzahl BLZ (8 Stellen)
          Position 13-22: Kontonummer (10 Stellen, kann führende Nullen haben)
          → Nimm nur die letzten 10 Zeichen der IBAN (ohne Leerzeichen), entferne führende Nullen.
          Beispiel: IBAN "DE90 7435 0000 0000 9980 87" → ohne Leerzeichen "DE90743500000000998087"
                    → letzte 10 Stellen: "0000998087" → ohne führende Nullen: 998087
          Beispiel: IBAN "DE27 7435 0000 0004 4585 32" → ohne Leerzeichen "DE27743500000004458532"
                    → letzte 10 Stellen: "0004458532" → ohne führende Nullen: 4458532
  Gib die Kontonummer OHNE Leerzeichen aus, also z.B.: Konto_998087 oder Konto_4458532
- Pfarrbrief: Wenn die Überschrift des Dokuments „Pfarrbrief" lautet, steht darunter (meist in Klammern)
  ein Gültigkeitszeitraum, z.B. „(23.03.2026 – 17.05.2026)". Benenne die Datei so:
  YYYY-MM-DD_bis_YYYY-MM-DD_Pfarrbrief{suffix}
  Beispiel: 2026-03-23_bis_2026-05-17_Pfarrbrief.pdf
  Kein weiteres Schlagwort, kein Betrag.
- Vereins-Jahreskalender: Wenn das Dokument ein Jahresprogramm oder Jahreskalender eines Vereins ist
  (z.B. Freiwillige Feuerwehr, Schützenverein, Sportverein), benenne die Datei so:
  YYYY_Vereinsname_Jahreskalender{suffix}
  YYYY: das Jahr des Kalenders (aus dem Dokument). Vereinsname: Kurzname ohne Leerzeichen und ohne Umlaute
  (z.B. FF_Hoelskofen für Freiwillige Feuerwehr Hölskofen, KP_Hoelskofen für Königstreue Patrioten Hölskofen).
  Kein Betrag, kein weiteres Schlagwort.
  Beispiele: 2026_FF_Hoelskofen_Jahreskalender.pdf · 2026_KP_Hoelskofen_Jahreskalender.pdf
- Rosengasse (Essenbach): Wenn das Dokument eine Adresse oder Verbrauchsstelle in der
  Rosengasse in Essenbach enthält (egal von welcher Firma/Behörde), füge das
  Hausnummer-Kürzel direkt vor dem Betrag ein (bzw. am Ende, falls kein Betrag).
  Kürzel: Rosengasse 16 → RoGa16, Rosengasse 16A → RoGa16A,
  Rosengasse 18 → RoGa18, Rosengasse 18A → RoGa18A.
  Schema: YYYY-MM-DD_Kategorie_Firma_Schlagwort_RoGa18A_-8.42€.pdf
- Bestandteile mit _ verbinden, keine Leerzeichen
- Dateiendung beibehalten: {suffix}"""

        if suffix == ".pdf":
            content = [
                {"type": "document", "source": {"type": "base64", "media_type": media_type, "data": data}},
                {"type": "text", "text": prompt},
            ]
        else:
            content = [
                {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": data}},
                {"type": "text", "text": prompt},
            ]

        client  = anthropic.Anthropic(api_key=CLAUDE_API_KEY)
        message = client.messages.create(
            model=MODEL,
            max_tokens=256,
            messages=[{"role": "user", "content": content}]
        )

        new_name = message.content[0].text.strip().strip('"').strip("'")

        if not re.match(r"^\d{4}[-_]", new_name):
            log(f"⚠️  Ungültiger Name: {new_name!r} – übersprungen")
            return

        folder   = str(Path(dropbox_path).parent)
        new_path = f"{folder}/{new_name}"
        dbx.files_move_v2(dropbox_path, new_path, autorename=False)
        log(f"✅  {filename}  →  {new_name}")

        if "pfarrbrief" in new_name.lower():
            log("📋  Pfarrbrief erkannt – starte Verarbeitung + Kalender-Upload")
            threading.Thread(target=lambda p=new_path: _process_pfarrbrief(dbx, p), daemon=True).start()
            threading.Thread(target=lambda p=new_path: _auto_upload_kalender(dbx, p), daemon=True).start()

        if "jahreskalender" in new_name.lower() or "veranstaltungskalender" in new_name.lower():
            log("📅  Veranstaltungskalender erkannt – starte Verarbeitung + Kalender-Upload")
            threading.Thread(target=lambda p=new_path: _process_verein(dbx, p), daemon=True).start()
            threading.Thread(target=lambda p=new_path: _auto_upload_kalender(dbx, p), daemon=True).start()

    finally:
        Path(tmp_path).unlink(missing_ok=True)


def _process_pfarrbrief(dbx: dropbox.Dropbox, dropbox_path: str) -> None:
    try:
        subprocess.Popen(
            ["/opt/rename-webhook/bin/python3", "/opt/rename-webhook/pfarrbrief_manager.py", dropbox_path],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
    except Exception as e:
        log(f"❌  Pfarrbrief-Verarbeitung fehlgeschlagen: {e}")


def _process_verein(dbx: dropbox.Dropbox, dropbox_path: str) -> None:
    try:
        subprocess.Popen(
            ["/opt/rename-webhook/bin/python3", "/opt/rename-webhook/verein_manager.py", dropbox_path],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
    except Exception as e:
        log(f"❌  Vereins-Verarbeitung fehlgeschlagen: {e}")


def _auto_upload_kalender(dbx: dropbox.Dropbox, dropbox_path: str) -> None:
    """Lädt eine PDF-Datei automatisch in den Vereinskalender hoch (kein HTTP-Self-Call)."""
    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
            tmp_path = tmp.name
        dbx.files_download_to_file(tmp_path, dropbox_path)
        pdf_bytes = Path(tmp_path).read_bytes()

        result   = import_pdf_bytes(pdf_bytes, ".pdf")
        alle     = result["alle"]
        auto_plz = result["auto_plz"]

        try:
            data = json.loads(VEREINSTERMINE_FILE.read_text()) if VEREINSTERMINE_FILE.exists() else {}
        except Exception:
            data = {}

        result_vereine, total = _do_save_import(alle, auto_plz, "", data)
        log(f"📥  Auto-Upload Kalender: {total} Termine, {len(result_vereine)} Vereine")
    except Exception as e:
        log(f"❌  Auto-Upload Kalender fehlgeschlagen: {e}")
    finally:
        if tmp_path:
            Path(tmp_path).unlink(missing_ok=True)


def process_changes(dbx: dropbox.Dropbox) -> None:
    cursor = get_cursor(dbx)
    result = dbx.files_list_folder_continue(cursor)

    while True:
        for entry in result.entries:
            if isinstance(entry, (DeletedMetadata, FolderMetadata)):
                continue
            if str(Path(entry.path_lower).parent) != WATCH_FOLDER.lower():
                continue
            try:
                rename_via_claude(dbx, entry.path_display)
            except Exception as e:
                log(f"❌  Fehler bei {entry.name}: {e}")

        save_cursor(result.cursor)

        if not result.has_more:
            break
        result = dbx.files_list_folder_continue(result.cursor)


def get_kalender_input_cursor(dbx: dropbox.Dropbox) -> str:
    if Path(KALENDER_CURSOR_FILE).exists():
        return Path(KALENDER_CURSOR_FILE).read_text().strip()
    result = dbx.files_list_folder(KALENDER_INPUT_FOLDER, recursive=False)
    cursor = result.cursor
    while result.has_more:
        result = dbx.files_list_folder_continue(cursor)
        cursor = result.cursor
    Path(KALENDER_CURSOR_FILE).write_text(cursor)
    log("🔖  Kalender-Input-Cursor initialisiert")
    return cursor


def save_kalender_input_cursor(cursor: str) -> None:
    Path(KALENDER_CURSOR_FILE).write_text(cursor)


def _process_kalender_input_file(dbx: dropbox.Dropbox, dropbox_path: str) -> None:
    """Lädt Datei herunter, extrahiert Termine via Claude, sendet Telegram-Bestätigung."""
    dateiname = Path(dropbox_path).name
    suffix    = Path(dateiname).suffix.lower()

    if suffix not in ALLOWED_EXTENSIONS:
        log(f"⏭️  Kalender-Input: Datei übersprungen (kein erlaubter Typ): {dateiname}")
        return

    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
            tmp_path = tmp.name
        dbx.files_download_to_file(tmp_path, dropbox_path)
        file_bytes = Path(tmp_path).read_bytes()

        log(f"📥  Kalender-Input: Verarbeite {dateiname}")
        result   = import_pdf_bytes(file_bytes, suffix)
        alle     = result["alle"]
        auto_plz = result.get("auto_plz", "")

        done_path = f"{KALENDER_DONE_FOLDER}/{dateiname}"
        try:
            dbx.files_move_v2(dropbox_path, done_path, autorename=True)
        except Exception as e:
            log(f"⚠️  Kalender-Input: Verschieben fehlgeschlagen: {e}")

        if not alle:
            send_telegram(TELEGRAM_CHAT_ID, f"📥 Kalender-Input: {dateiname}\n\n⚠️ Keine Termine gefunden.")
            return

        uid   = str(_uuid.uuid4())[:8]
        total = len(alle)
        preview_lines = []
        for t in sorted(alle, key=lambda x: x.get("datum", ""))[:5]:
            datum  = t.get("datum", "?")
            bez    = t.get("bezeichnung", "?")
            verein = t.get("verein", "")
            preview_lines.append(f"• {datum} – {verein}: {bez}")
        if total > 5:
            preview_lines.append(f"… (+{total - 5} weitere)")

        plz_info = f"\n📮 PLZ: {auto_plz}" if auto_plz else ""
        msg_text = (
            f"📥 Neue Termine-Datei: {dateiname}{plz_info}\n"
            f"📋 {total} Termine gefunden:\n\n"
            + "\n".join(preview_lines)
            + f"\n\n[ID: {uid}]"
        )
        keyboard = [[
            {"text": "✅ Importieren", "callback_data": f"kal_ok:{uid}"},
            {"text": "❌ Verwerfen",   "callback_data": f"kal_no:{uid}"},
        ]]
        _kalender_pending[uid] = {"alle": alle, "auto_plz": auto_plz, "dateiname": dateiname}
        send_telegram_inline(TELEGRAM_CHAT_ID, msg_text, keyboard)
        log(f"✅  Kalender-Input: {total} Termine extrahiert, warte auf Bestätigung [{uid}]")

    except Exception as e:
        log(f"❌  Kalender-Input fehlgeschlagen ({dateiname}): {e}")
        send_telegram(TELEGRAM_CHAT_ID, f"❌ Kalender-Input Fehler: {dateiname}\n{e}")
    finally:
        if tmp_path:
            Path(tmp_path).unlink(missing_ok=True)


def process_kalender_input_changes(dbx: dropbox.Dropbox) -> None:
    try:
        cursor = get_kalender_input_cursor(dbx)
    except Exception as e:
        log(f"⚠️  Kalender-Input-Cursor nicht abrufbar: {e}")
        return

    result = dbx.files_list_folder_continue(cursor)
    while True:
        for entry in result.entries:
            if isinstance(entry, (DeletedMetadata, FolderMetadata)):
                continue
            if str(Path(entry.path_lower).parent) != KALENDER_INPUT_FOLDER.lower():
                continue
            try:
                threading.Thread(
                    target=lambda p=entry.path_display: _process_kalender_input_file(dbx, p),
                    daemon=True,
                ).start()
            except Exception as e:
                log(f"❌  Kalender-Input Fehler bei {entry.name}: {e}")

        save_kalender_input_cursor(result.cursor)
        if not result.has_more:
            break
        result = dbx.files_list_folder_continue(result.cursor)


@rename_bp.route("/webhook", methods=["GET"])
def verify():
    challenge = request.args.get("challenge")
    if challenge:
        return challenge, 200, {"Content-Type": "text/plain"}
    abort(400)


@rename_bp.route("/webhook", methods=["POST"])
def webhook():
    signature = request.headers.get("X-Dropbox-Signature", "")
    expected  = hmac.new(
        DROPBOX_APP_SECRET.encode(),
        request.data,
        hashlib.sha256,
    ).hexdigest()
    if not hmac.compare_digest(signature, expected):
        log("⚠️  Ungültige Webhook-Signatur")
        abort(403)

    dbx = get_dropbox_client()
    process_changes(dbx)
    process_kalender_input_changes(dbx)
    return "", 200
