import json
import os

from flask import Blueprint, request

from shared.kalender_core import VEREINSTERMINE_FILE, log
from shared.vk_db import (
    tg_get_subscriptions, tg_subscribe, tg_unsubscribe, tg_unsubscribe_all
)

kalender_bot_bp = Blueprint("kalender_bot", __name__)

KALENDER_BOT_TOKEN = os.environ.get("KALENDER_BOT_TOKEN", "")


def _bot_send(chat_id, text):
    import urllib.request
    if not KALENDER_BOT_TOKEN:
        return
    payload = json.dumps({"chat_id": chat_id, "text": text, "parse_mode": "HTML"}).encode()
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{KALENDER_BOT_TOKEN}/sendMessage",
        data=payload,
        headers={"Content-Type": "application/json"},
    )
    try:
        urllib.request.urlopen(req, timeout=10)
    except Exception as e:
        log(f"❌ kalender_bot send: {e}")


def _bot_send_inline(chat_id, text, keyboard):
    import urllib.request
    if not KALENDER_BOT_TOKEN:
        return
    payload = json.dumps({
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "reply_markup": {"inline_keyboard": keyboard},
    }).encode()
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{KALENDER_BOT_TOKEN}/sendMessage",
        data=payload,
        headers={"Content-Type": "application/json"},
    )
    try:
        urllib.request.urlopen(req, timeout=10)
    except Exception as e:
        log(f"❌ kalender_bot send_inline: {e}")


def _bot_answer_callback(cb_id, text=""):
    import urllib.request
    if not KALENDER_BOT_TOKEN:
        return
    payload = json.dumps({"callback_query_id": cb_id, "text": text}).encode()
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{KALENDER_BOT_TOKEN}/answerCallbackQuery",
        data=payload,
        headers={"Content-Type": "application/json"},
    )
    try:
        urllib.request.urlopen(req, timeout=10)
    except Exception as e:
        log(f"❌ kalender_bot answer_callback: {e}")


def _load_verein_labels() -> dict:
    if not VEREINSTERMINE_FILE.exists():
        return {}
    try:
        data = json.loads(VEREINSTERMINE_FILE.read_text())
        return {k: v for k, v in data.get("_labels", {}).items()}
    except Exception:
        return {}


def _verein_auswahl_keyboard(chat_id: str, labels: dict) -> list:
    abos = set(tg_get_subscriptions(chat_id))
    buttons = []
    row = []
    for key, name in sorted(labels.items(), key=lambda x: x[1]):
        mark = "✅ " if key in abos else ""
        btn = {"text": f"{mark}{name}", "callback_data": f"vk_abo:{key}"}
        row.append(btn)
        if len(row) == 2:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)
    buttons.append([{"text": "✅ Fertig", "callback_data": "vk_fertig"}])
    return buttons


@kalender_bot_bp.route("/kalender-bot", methods=["POST"])
def kalender_bot_webhook():
    data = request.get_json(silent=True) or {}

    # ── Text-Nachrichten ─────────────────────────────────────────────────────
    message = data.get("message", {})
    chat_id = str(message.get("chat", {}).get("id", ""))
    text    = message.get("text", "").strip() if message else ""

    if chat_id and text:
        labels = _load_verein_labels()

        if text in ("/start", "/hilfe", "/help"):
            _bot_send_inline(chat_id,
                "👋 <b>Willkommen beim Vereinskalender-Bot!</b>\n\n"
                "Ich schicke dir jeden Abend um <b>18:00 Uhr</b> eine kurze Nachricht, "
                "wenn einer deiner Vereine am nächsten Tag einen Termin hat – "
                "damit du nichts verpasst.\n\n"
                "👇 Tippe auf <b>Vereine auswählen</b> und wähle die Vereine aus, "
                "für die du Erinnerungen erhalten möchtest.",
                [[{"text": "🏘️ Vereine auswählen", "callback_data": "vk_start_abo"}],
                 [{"text": "📋 Meine Abonnements", "callback_data": "vk_meineabos"}]]
            )

        elif text == "/abo":
            if not labels:
                _bot_send(chat_id, "⚠️ Noch keine Vereine im Kalender.")
            else:
                keyboard = _verein_auswahl_keyboard(chat_id, labels)
                _bot_send_inline(chat_id,
                    "🏘️ <b>Welche Vereine möchtest du abonnieren?</b>\n"
                    "Tippe auf einen Verein um ihn ab-/anzumelden.\n"
                    "✅ = bereits abonniert",
                    keyboard
                )

        elif text == "/meineabos":
            abos = tg_get_subscriptions(chat_id)
            if not abos:
                _bot_send(chat_id,
                    "Du hast noch keine Abonnements.\n"
                    "Tippe /abo um Vereine auszuwählen."
                )
            else:
                namen = [labels.get(k, k) for k in abos]
                liste = "\n".join(f"• {n}" for n in namen)
                _bot_send(chat_id,
                    f"📋 <b>Deine Abonnements ({len(abos)}):</b>\n\n{liste}\n\n"
                    "Mit /abo kannst du Vereine hinzufügen oder entfernen.\n"
                    "Mit /stop kannst du alle Abos löschen."
                )

        elif text in ("/stop", "/abbestellen"):
            count = tg_unsubscribe_all(chat_id)
            if count == 0:
                _bot_send(chat_id, "Du hattest keine aktiven Abonnements.")
            else:
                _bot_send(chat_id,
                    f"✅ {count} Abonnement(s) gelöscht.\n"
                    "Du erhältst keine Erinnerungen mehr.\n"
                    "Mit /abo kannst du dich jederzeit neu anmelden."
                )

        else:
            _bot_send(chat_id,
                "Ich verstehe diesen Befehl nicht.\n"
                "Tippe /hilfe für eine Übersicht."
            )

    # ── Inline-Keyboard Callbacks ────────────────────────────────────────────
    callback_query = data.get("callback_query", {})
    if callback_query:
        cb_id   = callback_query.get("id", "")
        cb_data = callback_query.get("data", "")
        cb_chat = str(callback_query.get("from", {}).get("id", ""))

        if cb_data in ("vk_start_abo", "vk_abo_oeffnen"):
            labels = _load_verein_labels()
            if not labels:
                _bot_answer_callback(cb_id, "⚠️ Noch keine Vereine im Kalender")
            else:
                _bot_answer_callback(cb_id)
                keyboard = _verein_auswahl_keyboard(cb_chat, labels)
                _bot_send_inline(cb_chat,
                    "🏘️ <b>Welche Vereine möchtest du abonnieren?</b>\n"
                    "Tippe auf einen Verein um ihn ab-/anzumelden.\n"
                    "✅ = bereits abonniert",
                    keyboard
                )

        elif cb_data == "vk_meineabos":
            abos = tg_get_subscriptions(cb_chat)
            labels = _load_verein_labels()
            _bot_answer_callback(cb_id)
            if not abos:
                _bot_send(cb_chat,
                    "Du hast noch keine Abonnements.\n"
                    "Tippe auf /abo um Vereine auszuwählen."
                )
            else:
                namen = [labels.get(k, k) for k in abos]
                liste = "\n".join(f"• {n}" for n in namen)
                _bot_send(cb_chat,
                    f"📋 <b>Deine Abonnements ({len(abos)}):</b>\n\n{liste}\n\n"
                    "Abos ändern: /abo · Alle löschen: /stop"
                )

        elif cb_data.startswith("vk_abo:"):
            verein_key = cb_data.split(":", 1)[1]
            labels = _load_verein_labels()
            verein_name = labels.get(verein_key, verein_key)

            abos = set(tg_get_subscriptions(cb_chat))
            if verein_key in abos:
                tg_unsubscribe(cb_chat, verein_key)
                _bot_answer_callback(cb_id, f"❌ {verein_name} abbestellt")
            else:
                tg_subscribe(cb_chat, verein_key)
                _bot_answer_callback(cb_id, f"✅ {verein_name} abonniert")

        elif cb_data == "vk_fertig":
            abos = tg_get_subscriptions(cb_chat)
            labels = _load_verein_labels()
            if not abos:
                _bot_answer_callback(cb_id, "Noch keine Abos ausgewählt")
                _bot_send(cb_chat, "Du hast noch keine Vereine ausgewählt.\nTippe /abo um die Auswahl erneut zu öffnen.")
            else:
                namen = [labels.get(k, k) for k in abos]
                liste = "\n".join(f"• {n}" for n in namen)
                _bot_answer_callback(cb_id, "Gespeichert!")
                _bot_send(cb_chat,
                    f"✅ <b>Gespeichert!</b>\n\n"
                    f"Du erhältst Erinnerungen für:\n{liste}\n\n"
                    "Du bekommst jeweils am Vortag eine Benachrichtigung.\n"
                    "Abos ändern: /abo · Alle löschen: /stop"
                )

    return "", 200
