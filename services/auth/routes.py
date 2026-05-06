import os
import secrets
import re
from datetime import datetime, timedelta
from functools import wraps

import bcrypt
from flask import Blueprint, make_response, redirect, request

from shared.vk_db import create_session, db_conn, delete_session, get_session_user, init_db
from shared.vk_mail import (
    send_rejected_email,
    send_reset_email,
    send_verify_email,
    send_welcome_email,
)
from shared.flask_notify import send_telegram_inline

auth_bp = Blueprint("auth", __name__)

UPLOAD_TOKEN = os.environ.get("UPLOAD_TOKEN", "")
MAX_LOGIN_ATTEMPTS = 5
LOCKOUT_MINUTES = 15

_CSS = """
<style>
:root{color-scheme:dark}
*{box-sizing:border-box}
body{background:#1c1c1e;color:#f2f2f7;font-family:-apple-system,sans-serif;
     max-width:480px;margin:0 auto;padding:1.5rem 1rem}
h1{font-size:1.4rem;margin:0 0 1.5rem}
label{display:block;font-size:.85rem;color:#aeaeb2;margin:.75rem 0 .25rem}
input,select,textarea{width:100%;padding:.75rem;border-radius:.625rem;
  border:1px solid #3a3a3c;background:#2c2c2e;color:#f2f2f7;font-size:1rem}
input:focus,textarea:focus{outline:2px solid #0a84ff;border-color:transparent}
.btn{display:block;width:100%;padding:.875rem;margin-top:1rem;border:none;
     border-radius:.625rem;background:#0a84ff;color:#fff;font-size:1rem;
     font-weight:600;cursor:pointer;text-align:center;text-decoration:none}
.btn-sec{background:#2c2c2e;color:#0a84ff}
.btn-danger{background:#ff3b30}
.err{color:#ff453a;margin:.5rem 0;font-size:.9rem}
.ok{color:#34c759;margin:.5rem 0;font-size:.9rem}
.hint{color:#8e8e93;font-size:.82rem;margin:.5rem 0}
a{color:#0a84ff}
.card{background:#2c2c2e;border-radius:.875rem;padding:1rem;margin:.75rem 0}
.spam-hint{background:#2c2c2e;border-radius:.625rem;padding:.75rem 1rem;
           color:#aeaeb2;font-size:.85rem;margin-top:1rem}
hr{border:none;border-top:1px solid #3a3a3c;margin:1.5rem 0}
.chk{display:flex;gap:.75rem;align-items:flex-start;margin:.75rem 0}
.chk input{width:1.2rem;height:1.2rem;flex-shrink:0;margin-top:.15rem}
.chk label{margin:0;font-size:.9rem;color:#f2f2f7}
</style>
"""

_BACK = '<a class="btn btn-sec" href="/verein/login" style="margin-top:.75rem">← Zurück zum Login</a>'


def _page(title: str, body: str) -> str:
    return f"""<!doctype html><html lang="de"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title} – Vereinskalender</title>{_CSS}</head>
<body><h1>{title}</h1>{body}</body></html>"""


def _session_token() -> str:
    return request.cookies.get("vk_session", "")


def require_verein_login(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        user = get_session_user(_session_token())
        if not user:
            return redirect("/verein/login")
        if not user["email_verified"]:
            return redirect("/verein/login?hint=verify")
        if user["verein_status"] not in ("aktiv",):
            return redirect("/verein/login?hint=pending")
        return f(*args, user=user, **kwargs)
    return decorated


def _hash_pw(pw: str) -> str:
    return bcrypt.hashpw(pw.encode(), bcrypt.gensalt()).decode()


def _check_pw(pw: str, hashed: str) -> bool:
    return bcrypt.checkpw(pw.encode(), hashed.encode())


def _valid_email(email: str) -> bool:
    return bool(re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", email))


def _telegram_approve_msg(verein_id: int, verein_name: str, email: str) -> None:
    try:
        send_telegram_inline(
            os.environ.get("CHAT_ID", ""),
            f"🏛 Neuer Verein wartet auf Freigabe:\n<b>{verein_name}</b>\nKontakt: {email}",
            [
                [
                    {"text": "✅ Freigeben", "callback_data": f"verein_approve:{verein_id}"},
                    {"text": "❌ Ablehnen", "callback_data": f"verein_reject:{verein_id}"},
                ]
            ],
        )
    except Exception:
        pass


# ── Register ────────────────────────────────────────────────────────────────

@auth_bp.route("/verein/register", methods=["GET", "POST"])
def register():
    error = ""
    if request.method == "POST":
        verein_name = request.form.get("verein_name", "").strip()
        email = request.form.get("email", "").strip().lower()
        pw = request.form.get("password", "")
        pw2 = request.form.get("password2", "")
        sv = request.form.get("selbstverpflichtung", "")

        if not verein_name or len(verein_name) < 3:
            error = "Bitte einen Vereinsnamen mit mindestens 3 Zeichen eingeben."
        elif not _valid_email(email):
            error = "Bitte eine gültige E-Mail-Adresse eingeben."
        elif len(pw) < 8:
            error = "Passwort muss mindestens 8 Zeichen haben."
        elif pw != pw2:
            error = "Passwörter stimmen nicht überein."
        elif not sv:
            error = "Bitte die Selbstverpflichtungserklärung bestätigen."
        else:
            with db_conn() as conn:
                ex = conn.execute(
                    "SELECT id FROM vk_users WHERE email = ?", (email,)
                ).fetchone()
                if ex:
                    error = "Diese E-Mail-Adresse ist bereits registriert."
                else:
                    verein_row = conn.execute(
                        "INSERT INTO vereine_accounts (verein_name, selbstverpflichtung) VALUES (?,?) RETURNING id",
                        (verein_name, 1),
                    ).fetchone()
                    verein_id = verein_row["id"]
                    token = secrets.token_urlsafe(32)
                    expires = (datetime.utcnow() + timedelta(hours=24)).isoformat()
                    conn.execute(
                        """INSERT INTO vk_users
                           (email, password_hash, verein_id, role, verify_token, verify_token_expires)
                           VALUES (?,?,?,?,?,?)""",
                        (email, _hash_pw(pw), verein_id, "admin", token, expires),
                    )
            if not error:
                send_verify_email(email, token)
                _telegram_approve_msg(verein_id, verein_name, email)
                body = f"""
<p class="ok">✅ Registrierung eingegangen!</p>
<p>Wir haben dir eine E-Mail an <strong>{email}</strong> geschickt. Bitte bestätige deine Adresse.
Danach prüft der Administrator deine Anfrage (in der Regel innerhalb eines Tages).</p>
<div class="spam-hint">📬 Keine E-Mail erhalten? Bitte auch im <strong>Spam-Ordner</strong> nachsehen.</div>
<a class="btn btn-sec" href="/" style="margin-top:1rem">← Zum Kalender</a>"""
                return _page("Registrierung eingegangen", body)

    form = f"""
<p style="color:#aeaeb2;font-size:.9rem">Trage deinen Verein im Vereinskalender ein.</p>
{'<p class="err">'+error+'</p>' if error else ''}
<form method="post" autocomplete="on">
  <label>Vereinsname</label>
  <input name="verein_name" type="text" required autocomplete="organization" placeholder="z.B. FF Musterdorf">
  <label>E-Mail (Ansprechpartner)</label>
  <input name="email" type="text" inputmode="email" autocorrect="off" autocapitalize="none" required autocomplete="email" placeholder="vorstand@beispiel.de">
  <label>Passwort <span class="hint">(mind. 8 Zeichen)</span></label>
  <input name="password" type="password" required autocomplete="new-password">
  <label>Passwort wiederholen</label>
  <input name="password2" type="password" required autocomplete="new-password">
  <div class="chk">
    <input type="checkbox" name="selbstverpflichtung" id="sv">
    <label for="sv">Ich bestätige, dass ich befugter Vertreter des genannten Vereins bin (gewählter Vorstand oder schriftlich bevollmächtigtes Mitglied) und berechtigt bin, im Namen des Vereins Termine zu veröffentlichen. Ich übernehme die Verantwortung für die Richtigkeit der eingetragenen Daten.</label>
  </div>
  <button class="btn" type="submit">Registrieren</button>
</form>
<hr>
<p class="hint">Bereits registriert? <a href="/verein/login">Zum Login</a></p>
<p class="hint"><a href="/verein/datenschutz">Datenschutzerklärung</a> · <a href="/verein/nutzungsbedingungen">Nutzungsbedingungen</a></p>"""
    return _page("Verein registrieren", form)


# ── E-Mail verifizieren ──────────────────────────────────────────────────────

@auth_bp.route("/api/auth/verify")
def verify_email():
    token = request.args.get("token", "")
    with db_conn() as conn:
        row = conn.execute(
            "SELECT id, verify_token_expires FROM vk_users WHERE verify_token = ?",
            (token,),
        ).fetchone()
        if not row:
            body = f'<p class="err">Ungültiger Bestätigungslink.</p><a href="/api/auth/resend-verify">Neuen Link anfordern</a>{_BACK}'
            return _page("Fehler", body), 400
        if datetime.fromisoformat(row["verify_token_expires"]) < datetime.utcnow():
            body = f'<p class="err">Der Bestätigungslink ist abgelaufen.</p><a class="btn" href="/api/auth/resend-verify">Neuen Bestätigungslink anfordern</a>'
            return _page("Link abgelaufen", body), 400
        conn.execute(
            "UPDATE vk_users SET email_verified=1, verify_token=NULL, verify_token_expires=NULL WHERE id=?",
            (row["id"],),
        )
    body = f'<p class="ok">✅ E-Mail-Adresse bestätigt!</p><p>Dein Konto wird nun vom Administrator geprüft. Du erhältst eine E-Mail sobald es freigeschaltet wurde.</p><a class="btn btn-sec" href="/">← Zum Kalender</a>'
    return _page("Bestätigt", body)


@auth_bp.route("/api/auth/resend-verify", methods=["GET", "POST"])
def resend_verify():
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        with db_conn() as conn:
            row = conn.execute(
                "SELECT id FROM vk_users WHERE email=? AND email_verified=0", (email,)
            ).fetchone()
            if row:
                token = secrets.token_urlsafe(32)
                expires = (datetime.utcnow() + timedelta(hours=24)).isoformat()
                conn.execute(
                    "UPDATE vk_users SET verify_token=?, verify_token_expires=? WHERE id=?",
                    (token, expires, row["id"]),
                )
                send_verify_email(email, token)
        body = '<p class="ok">Falls die E-Mail existiert und noch nicht bestätigt ist, wurde ein neuer Link verschickt.</p><div class="spam-hint">📬 Bitte auch im <strong>Spam-Ordner</strong> nachsehen.</div>' + _BACK
        return _page("Link verschickt", body)
    form = f'<form method="post"><label>E-Mail-Adresse</label><input name="email" type="email" required><button class="btn" type="submit">Neuen Link anfordern</button></form>{_BACK}'
    return _page("Bestätigungslink anfordern", form)


# ── Login ────────────────────────────────────────────────────────────────────

@auth_bp.route("/verein/login", methods=["GET", "POST"])
def login():
    hint = request.args.get("hint", "")
    hint_msg = {
        "verify": '<p class="hint">Bitte bestätige zuerst deine E-Mail-Adresse.</p>',
        "pending": '<p class="hint">Dein Konto wartet noch auf Freigabe durch den Administrator.</p>',
        "reset": '<p class="ok">Passwort wurde geändert. Bitte jetzt einloggen.</p>',
    }.get(hint, "")

    error = ""
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        pw = request.form.get("password", "")
        with db_conn() as conn:
            row = conn.execute(
                """SELECT u.id, u.password_hash, u.aktiv, u.email_verified,
                          u.login_attempts, u.locked_until, u.role,
                          v.status as verein_status
                   FROM vk_users u
                   JOIN vereine_accounts v ON v.id = u.verein_id
                   WHERE u.email = ?""",
                (email,),
            ).fetchone()

            if not row:
                error = "E-Mail oder Passwort falsch."
            else:
                if row["locked_until"] and datetime.fromisoformat(row["locked_until"]) > datetime.utcnow():
                    error = f"Zu viele Fehlversuche. Bitte {LOCKOUT_MINUTES} Minuten warten."
                elif not _check_pw(pw, row["password_hash"]):
                    attempts = row["login_attempts"] + 1
                    locked = None
                    if attempts >= MAX_LOGIN_ATTEMPTS:
                        locked = (datetime.utcnow() + timedelta(minutes=LOCKOUT_MINUTES)).isoformat()
                    conn.execute(
                        "UPDATE vk_users SET login_attempts=?, locked_until=? WHERE id=?",
                        (attempts, locked, row["id"]),
                    )
                    error = "E-Mail oder Passwort falsch."
                elif not row["aktiv"]:
                    error = "Dein Konto ist deaktiviert."
                elif not row["email_verified"]:
                    return redirect("/verein/login?hint=verify")
                elif row["verein_status"] != "aktiv":
                    return redirect("/verein/login?hint=pending")
                else:
                    conn.execute(
                        "UPDATE vk_users SET login_attempts=0, locked_until=NULL WHERE id=?",
                        (row["id"],),
                    )
                    session_token = create_session(row["id"])
                    resp = make_response(redirect("/verein/dashboard"))
                    resp.set_cookie(
                        "vk_session", session_token,
                        httponly=True, samesite="Lax",
                        max_age=SESSION_TIMEOUT_HOURS * 3600,
                    )
                    return resp

    form = f"""
{hint_msg}
{'<p class="err">'+error+'</p>' if error else ''}
<form method="post" autocomplete="on">
  <label>E-Mail</label>
  <input name="email" type="email" required autocomplete="email">
  <label>Passwort</label>
  <input name="password" type="password" required autocomplete="current-password">
  <button class="btn" type="submit">Einloggen</button>
</form>
<hr>
<p class="hint"><a href="/verein/passwort-vergessen">Passwort vergessen?</a></p>
<p class="hint">Noch kein Konto? <a href="/verein/register">Verein registrieren</a></p>"""
    return _page("Login", form)


# ── Logout ───────────────────────────────────────────────────────────────────

@auth_bp.route("/verein/logout", methods=["POST"])
def logout():
    token = _session_token()
    if token:
        delete_session(token)
    resp = make_response(redirect("/"))
    resp.delete_cookie("vk_session")
    return resp


# ── Passwort vergessen / Reset ───────────────────────────────────────────────

@auth_bp.route("/verein/passwort-vergessen", methods=["GET", "POST"])
def forgot_password():
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        with db_conn() as conn:
            row = conn.execute(
                "SELECT id FROM vk_users WHERE email = ?", (email,)
            ).fetchone()
            if row:
                token = secrets.token_urlsafe(32)
                expires = (datetime.utcnow() + timedelta(hours=1)).isoformat()
                conn.execute(
                    "UPDATE vk_users SET reset_token=?, reset_token_expires=? WHERE id=?",
                    (token, expires, row["id"]),
                )
                send_reset_email(email, token)
        body = '<p class="ok">Falls diese E-Mail registriert ist, wurde ein Reset-Link verschickt.</p><div class="spam-hint">📬 Bitte auch im <strong>Spam-Ordner</strong> nachsehen.</div>' + _BACK
        return _page("Link verschickt", body)
    form = f'<p style="color:#aeaeb2">Gib deine E-Mail-Adresse ein. Du erhältst einen Link zum Passwort-Zurücksetzen.</p><form method="post"><label>E-Mail</label><input name="email" type="email" required><button class="btn" type="submit">Reset-Link anfordern</button></form>{_BACK}'
    return _page("Passwort vergessen", form)


@auth_bp.route("/verein/passwort-reset", methods=["GET", "POST"])
def reset_password():
    token = request.args.get("token", "") or request.form.get("token", "")
    with db_conn() as conn:
        row = conn.execute(
            "SELECT id, reset_token_expires FROM vk_users WHERE reset_token = ?",
            (token,),
        ).fetchone()
        if not row:
            body = f'<p class="err">Ungültiger oder bereits verwendeter Reset-Link.</p>{_BACK}'
            return _page("Fehler", body), 400
        if datetime.fromisoformat(row["reset_token_expires"]) < datetime.utcnow():
            body = f'<p class="err">Der Reset-Link ist abgelaufen.</p><a class="btn" href="/verein/passwort-vergessen">Neuen Link anfordern</a>'
            return _page("Link abgelaufen", body), 400

        if request.method == "POST":
            pw = request.form.get("password", "")
            pw2 = request.form.get("password2", "")
            if len(pw) < 8:
                pass
            elif pw != pw2:
                pass
            else:
                conn.execute(
                    "UPDATE vk_users SET password_hash=?, reset_token=NULL, reset_token_expires=NULL, login_attempts=0, locked_until=NULL WHERE id=?",
                    (_hash_pw(pw), row["id"]),
                )
                return redirect("/verein/login?hint=reset")

    form = f"""<form method="post">
<input type="hidden" name="token" value="{token}">
<label>Neues Passwort <span class="hint">(mind. 8 Zeichen)</span></label>
<input name="password" type="password" required autocomplete="new-password">
<label>Passwort wiederholen</label>
<input name="password2" type="password" required autocomplete="new-password">
<button class="btn" type="submit">Passwort speichern</button>
</form>"""
    return _page("Neues Passwort", form)


# ── Superadmin: Verein freigeben/ablehnen ────────────────────────────────────

@auth_bp.route("/api/admin/vereine/<int:verein_id>/approve", methods=["POST"])
def approve_verein(verein_id: int):
    token = request.headers.get("X-Upload-Token", "")
    if not UPLOAD_TOKEN or token != UPLOAD_TOKEN:
        return {"error": "Unauthorized"}, 401
    with db_conn() as conn:
        row = conn.execute(
            """SELECT v.verein_name, u.email FROM vereine_accounts v
               JOIN vk_users u ON u.verein_id = v.id
               WHERE v.id = ? AND v.status = 'pending'""",
            (verein_id,),
        ).fetchone()
        if not row:
            return {"error": "Nicht gefunden oder bereits bearbeitet"}, 404
        conn.execute(
            "UPDATE vereine_accounts SET status='aktiv', freigegeben_at=CURRENT_TIMESTAMP WHERE id=?",
            (verein_id,),
        )
        send_welcome_email(row["email"], row["verein_name"])
    return {"ok": True}


@auth_bp.route("/api/admin/vereine/<int:verein_id>/reject", methods=["POST"])
def reject_verein(verein_id: int):
    token = request.headers.get("X-Upload-Token", "")
    if not UPLOAD_TOKEN or token != UPLOAD_TOKEN:
        return {"error": "Unauthorized"}, 401
    with db_conn() as conn:
        row = conn.execute(
            """SELECT v.verein_name, u.email FROM vereine_accounts v
               JOIN vk_users u ON u.verein_id = v.id
               WHERE v.id = ? AND v.status = 'pending'""",
            (verein_id,),
        ).fetchone()
        if not row:
            return {"error": "Nicht gefunden oder bereits bearbeitet"}, 404
        conn.execute(
            "UPDATE vereine_accounts SET status='abgelehnt' WHERE id=?",
            (verein_id,),
        )
        send_rejected_email(row["email"], row["verein_name"])
    return {"ok": True}


@auth_bp.route("/api/admin/vereine-pending")
def pending_vereine():
    token = request.headers.get("X-Upload-Token", "")
    if not UPLOAD_TOKEN or token != UPLOAD_TOKEN:
        return {"error": "Unauthorized"}, 401
    with db_conn() as conn:
        rows = conn.execute(
            """SELECT v.id, v.verein_name, v.created_at, u.email
               FROM vereine_accounts v
               JOIN vk_users u ON u.verein_id = v.id AND u.role='admin'
               WHERE v.status='pending' ORDER BY v.created_at""",
        ).fetchall()
    return [dict(r) for r in rows]




# ── Session-Status (für Client-JS) ──────────────────────────────────────────

@auth_bp.route("/api/auth/me")
def auth_me():
    user = get_session_user(_session_token())
    if not user or not user["email_verified"] or user["verein_status"] != "aktiv":
        return {"loggedin": False}, 200
    return {
        "loggedin": True,
        "role": user["role"],
        "verein_name": user["verein_name"],
        "email": user["email"],
    }, 200

# ── DB init on import ────────────────────────────────────────────────────────

init_db()
