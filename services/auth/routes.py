import os
import secrets
import re
import threading
from datetime import datetime, timedelta
from functools import wraps

import bcrypt
from flask import Blueprint, make_response, redirect, request

from shared.vk_db import SESSION_TIMEOUT_HOURS, create_session, db_conn, delete_session, get_session_user, init_db
from shared.kalender_core import lookup_plz
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
RUBRIKEN = ["Verein", "Pfarrei", "Kunst und Kultur", "Sonstiges"]

# Kurzlebiger Pre-Auth-Store für Vereinsauswahl bei mehreren Accounts pro E-Mail
# token → ([(user_id, verein_name)], expires)
_preauth: dict[str, tuple[list[tuple[int, str]], datetime]] = {}
_preauth_lock = threading.Lock()


def _make_preauth(choices: list[tuple[int, str]]) -> str:
    token = secrets.token_urlsafe(16)
    with _preauth_lock:
        _preauth[token] = (choices, datetime.utcnow() + timedelta(minutes=5))
    return token


def _pop_preauth(token: str) -> list[tuple[int, str]] | None:
    with _preauth_lock:
        entry = _preauth.pop(token, None)
    if not entry:
        return None
    choices, expires = entry
    return choices if datetime.utcnow() < expires else None

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


def _telegram_approve_msg(verein_id: int, verein_name: str, email: str,
                           rubrik: str = "", heimatort: str = "", telefon: str = "") -> None:
    lines = [f"🏛 Neuer Verein wartet auf Freigabe:\n<b>{verein_name}</b>"]
    if rubrik:
        lines.append(f"Rubrik: {rubrik}")
    if heimatort:
        lines.append(f"Ort: {heimatort}")
    lines.append(f"E-Mail: {email}")
    if telefon:
        lines.append(f"Telefon: {telefon}")
    try:
        send_telegram_inline(
            os.environ.get("CHAT_ID", ""),
            "\n".join(lines),
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
    form_data: dict = {}
    if request.method == "POST":
        verein_name = request.form.get("verein_name", "").strip()
        email       = request.form.get("email", "").strip().lower()
        pw          = request.form.get("password", "")
        pw2         = request.form.get("password2", "")
        sv          = request.form.get("selbstverpflichtung", "")
        rubrik      = request.form.get("rubrik", "").strip()
        heimatort   = request.form.get("heimatort", "").strip()
        plz         = request.form.get("plz", "").strip()
        telefon     = request.form.get("telefon", "").strip()
        form_data   = dict(verein_name=verein_name, email=email, rubrik=rubrik,
                           heimatort=heimatort, plz=plz, telefon=telefon)

        if not verein_name or len(verein_name) < 3:
            error = "Bitte einen Vereinsnamen mit mindestens 3 Zeichen eingeben."
        elif not _valid_email(email):
            error = "Bitte eine gültige E-Mail-Adresse eingeben."
        elif rubrik not in RUBRIKEN:
            error = "Bitte eine gültige Rubrik auswählen."
        elif not heimatort or len(heimatort) < 2:
            error = "Bitte den Heimatort des Vereins angeben."
        elif plz and not re.match(r"^\d{5}$", plz):
            error = "PLZ muss 5 Ziffern haben (z.B. 83308)."
        elif len(pw) < 8:
            error = "Passwort muss mindestens 8 Zeichen haben."
        elif pw != pw2:
            error = "Passwörter stimmen nicht überein."
        elif not sv:
            error = "Bitte die Selbstverpflichtungserklärung bestätigen."
        else:
            gemeinde = landkreis = ""
            if plz:
                geo = lookup_plz(plz)
                gemeinde  = geo.get("gemeinde", "")
                landkreis = geo.get("landkreis", "")
            with db_conn() as conn:
                verein_row = conn.execute(
                    """INSERT INTO vereine_accounts
                       (verein_name, selbstverpflichtung, rubrik, heimatort, plz, gemeinde, landkreis)
                       VALUES (?,?,?,?,?,?,?) RETURNING id""",
                    (verein_name, 1, rubrik, heimatort, plz or None, gemeinde or None, landkreis or None),
                ).fetchone()
                verein_id = verein_row["id"]
                token   = secrets.token_urlsafe(32)
                expires = (datetime.utcnow() + timedelta(hours=24)).isoformat()
                conn.execute(
                    """INSERT INTO vk_users
                       (email, password_hash, verein_id, role, telefon, verify_token, verify_token_expires)
                       VALUES (?,?,?,?,?,?,?)""",
                    (email, _hash_pw(pw), verein_id, "admin", telefon or None, token, expires),
                )
            send_verify_email(email, token)
            _telegram_approve_msg(verein_id, verein_name, email,
                                  rubrik=rubrik, heimatort=heimatort, telefon=telefon)
            body = f"""
<p class="ok">✅ Registrierung eingegangen!</p>
<p>Wir haben dir eine E-Mail an <strong>{email}</strong> geschickt. Bitte bestätige deine Adresse.
Danach prüft der Administrator deine Anfrage (in der Regel innerhalb eines Tages).</p>
<div class="spam-hint">📬 Keine E-Mail erhalten? Bitte auch im <strong>Spam-Ordner</strong> nachsehen.</div>
<a class="btn btn-sec" href="/" style="margin-top:.5rem">← Zurück zum Kalender</a>"""
            return _page("Registrierung eingegangen", body)

    rubrik_opts = "".join(
        f'<option value="{r}"{" selected" if form_data.get("rubrik") == r else ""}>{r}</option>'
        for r in RUBRIKEN
    )
    form = f"""
<p style="color:#aeaeb2;font-size:.9rem">Trage deinen Verein im Vereinskalender ein.</p>
{'<p class="err">'+error+'</p>' if error else ''}
<form method="post" autocomplete="on">
  <label>Vereinsname</label>
  <input name="verein_name" type="text" required autocomplete="organization" placeholder="z.B. FF Musterdorf" value="{form_data.get('verein_name', '')}">
  <label>Rubrik</label>
  <select name="rubrik" required>
    <option value="">– bitte wählen –</option>
    {rubrik_opts}
  </select>
  <label>Heimatort</label>
  <input name="heimatort" type="text" required placeholder="z.B. Musterdorf" value="{form_data.get('heimatort', '')}">
  <label>PLZ <span class="hint">(optional – für automatische Ortszuordnung)</span></label>
  <input name="plz" type="text" inputmode="numeric" maxlength="5" placeholder="z.B. 83308" value="{form_data.get('plz', '')}">
  <label>E-Mail (Ansprechpartner)</label>
  <input name="email" type="text" inputmode="email" autocorrect="off" autocapitalize="none" required autocomplete="email" placeholder="vorstand@beispiel.de" value="{form_data.get('email', '')}">
  <label>Telefon Ansprechpartner <span class="hint">(optional – für Rückfragen bei der Freigabe)</span></label>
  <input name="telefon" type="tel" autocomplete="tel" placeholder="z.B. 0172 1234567" value="{form_data.get('telefon', '')}">
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
<a class="btn btn-sec" href="/verein/login" style="margin-top:.5rem">← Abbrechen</a>
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
    body = f'<p class="ok">✅ E-Mail-Adresse bestätigt!</p><p>Dein Konto wird nun vom Administrator geprüft. Du erhältst eine E-Mail sobald es freigeschaltet wurde.</p><a class="btn btn-sec" href="/" style="margin-top:.5rem">← Zurück zum Kalender</a>'
    return _page("Bestätigt", body)


@auth_bp.route("/api/auth/resend-verify", methods=["GET", "POST"])
def resend_verify():
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        tokens_to_send = []
        with db_conn() as conn:
            rows = conn.execute(
                "SELECT id FROM vk_users WHERE email=? AND email_verified=0", (email,)
            ).fetchall()
            for row in rows:
                token = secrets.token_urlsafe(32)
                expires = (datetime.utcnow() + timedelta(hours=24)).isoformat()
                conn.execute(
                    "UPDATE vk_users SET verify_token=?, verify_token_expires=? WHERE id=?",
                    (token, expires, row["id"]),
                )
                tokens_to_send.append(token)
        for token in tokens_to_send:
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
        "verify":  '<p class="hint">Bitte bestätige zuerst deine E-Mail-Adresse.</p>',
        "pending": '<p class="hint">Dein Konto wartet noch auf Freigabe durch den Administrator.</p>',
        "reset":   '<p class="ok">Passwort wurde geändert. Bitte jetzt einloggen.</p>',
    }.get(hint, "")

    error = ""
    login_user_id = None

    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        pw    = request.form.get("password", "")
        now   = datetime.utcnow()

        with db_conn() as conn:
            rows = conn.execute(
                """SELECT u.id, u.password_hash, u.aktiv, u.email_verified,
                          u.login_attempts, u.locked_until, u.role,
                          v.status as verein_status, v.verein_name
                   FROM vk_users u
                   JOIN vereine_accounts v ON v.id = u.verein_id
                   WHERE u.email = ?""",
                (email,),
            ).fetchall()

            if not rows:
                error = "E-Mail oder Passwort falsch."
            else:
                # Wenn irgendein Account für diese E-Mail gesperrt ist → Lockout
                if any(r["locked_until"] and datetime.fromisoformat(r["locked_until"]) > now for r in rows):
                    error = f"Zu viele Fehlversuche. Bitte {LOCKOUT_MINUTES} Minuten warten."
                else:
                    matched = [r for r in rows if _check_pw(pw, r["password_hash"])]

                    if not matched:
                        # Fehlversuch: alle Rows dieser E-Mail hochzählen und ggf. sperren
                        new_attempts = max(r["login_attempts"] for r in rows) + 1
                        locked = (now + timedelta(minutes=LOCKOUT_MINUTES)).isoformat() if new_attempts >= MAX_LOGIN_ATTEMPTS else None
                        ids = [r["id"] for r in rows]
                        conn.execute(
                            f"UPDATE vk_users SET login_attempts=?, locked_until=? WHERE id IN ({','.join('?'*len(ids))})",
                            [new_attempts, locked] + ids,
                        )
                        error = "E-Mail oder Passwort falsch."
                    else:
                        # Erfolg: Zähler aller gematchten Rows zurücksetzen
                        matched_ids = [r["id"] for r in matched]
                        conn.execute(
                            f"UPDATE vk_users SET login_attempts=0, locked_until=NULL WHERE id IN ({','.join('?'*len(matched_ids))})",
                            matched_ids,
                        )
                        # Nur vollständig nutzbare Accounts weiter beachten
                        usable = [r for r in matched if r["aktiv"] and r["email_verified"] and r["verein_status"] == "aktiv"]

                        if not usable:
                            if any(not r["aktiv"] for r in matched):
                                error = "Dein Konto ist deaktiviert."
                            elif any(not r["email_verified"] for r in matched):
                                return redirect("/verein/login?hint=verify")
                            else:
                                return redirect("/verein/login?hint=pending")
                        elif len(usable) == 1:
                            login_user_id = usable[0]["id"]
                        else:
                            # Mehrere Vereine für diese E-Mail → Auswahl anbieten
                            choices = [(r["id"], r["verein_name"]) for r in usable]
                            preauth_token = _make_preauth(choices)
                            resp = make_response(redirect("/verein/login/verein-waehlen"))
                            resp.set_cookie("vk_preauth", preauth_token, httponly=True, samesite="Lax", max_age=300)
                            return resp

        if login_user_id is not None:
            session_token = create_session(login_user_id)
            resp = make_response(redirect("/verein/dashboard"))
            resp.set_cookie("vk_session", session_token, httponly=True, samesite="Lax", max_age=SESSION_TIMEOUT_HOURS * 3600)
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
<p class="hint">Noch kein Konto? <a href="/verein/register">Verein registrieren</a></p>
<a class="btn btn-sec" href="/" style="margin-top:.5rem">← Zurück zum Kalender</a>
<p class="hint" style="margin-top:1rem"><a href="/verein/datenschutz">Datenschutzerklärung</a> · <a href="/verein/nutzungsbedingungen">Nutzungsbedingungen</a></p>"""
    return _page("Login", form)


@auth_bp.route("/verein/login/verein-waehlen", methods=["GET", "POST"])
def login_verein_waehlen():
    preauth_token = request.cookies.get("vk_preauth", "")
    choices = _pop_preauth(preauth_token)
    if not choices:
        return redirect("/verein/login")

    if request.method == "POST":
        try:
            chosen_id = int(request.form.get("user_id", ""))
        except ValueError:
            return redirect("/verein/login")
        valid_ids = [uid for uid, _ in choices]
        if chosen_id not in valid_ids:
            return redirect("/verein/login")
        session_token = create_session(chosen_id)
        resp = make_response(redirect("/verein/dashboard"))
        resp.set_cookie("vk_session", session_token, httponly=True, samesite="Lax", max_age=SESSION_TIMEOUT_HOURS * 3600)
        resp.delete_cookie("vk_preauth")
        return resp

    # GET: Auswahl-Seite anzeigen
    options = "".join(
        f"""<form method="post" style="margin:.5rem 0">
  <input type="hidden" name="user_id" value="{uid}">
  <button class="btn" type="submit">{verein_name}</button>
</form>"""
        for uid, verein_name in choices
    )
    body = f"""
<p style="color:#aeaeb2;font-size:.9rem">Deine E-Mail-Adresse ist für mehrere Vereine registriert. Für welchen möchtest du dich einloggen?</p>
{options}
<hr>
<a class="btn btn-sec" href="/verein/login">← Zurück</a>"""
    return _page("Verein wählen", body)


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
        tokens_to_send = []
        with db_conn() as conn:
            rows = conn.execute(
                "SELECT id FROM vk_users WHERE email = ?", (email,)
            ).fetchall()
            for row in rows:
                token = secrets.token_urlsafe(32)
                expires = (datetime.utcnow() + timedelta(hours=1)).isoformat()
                conn.execute(
                    "UPDATE vk_users SET reset_token=?, reset_token_expires=? WHERE id=?",
                    (token, expires, row["id"]),
                )
                tokens_to_send.append(token)
        for token in tokens_to_send:
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


@auth_bp.route("/api/admin/users")
def admin_users():
    token = request.headers.get("X-Upload-Token", "")
    if not UPLOAD_TOKEN or token != UPLOAD_TOKEN:
        return {"error": "Unauthorized"}, 401
    with db_conn() as conn:
        vereine = conn.execute(
            """SELECT v.id, v.verein_key, v.verein_name, v.status,
                      v.rubrik, v.heimatort, v.plz, v.gemeinde, v.landkreis,
                      v.created_at
               FROM vereine_accounts v
               ORDER BY v.verein_name""",
        ).fetchall()
        users = conn.execute(
            """SELECT u.id, u.email, u.name, u.telefon, u.aktiv,
                      u.email_verified, u.created_at, u.role, u.verein_id
               FROM vk_users u""",
        ).fetchall()
    users_by_verein: dict = {}
    for u in users:
        users_by_verein.setdefault(u["verein_id"], []).append(dict(u))
    result = []
    for v in vereine:
        vd = dict(v)
        vd["users"] = users_by_verein.get(v["id"], [])
        result.append(vd)
    return result


@auth_bp.route("/api/admin/users/<int:user_id>", methods=["PATCH"])
def admin_update_user(user_id: int):
    token = request.headers.get("X-Upload-Token", "")
    if not UPLOAD_TOKEN or token != UPLOAD_TOKEN:
        return {"error": "Unauthorized"}, 401
    body = request.get_json(silent=True) or {}
    name    = body.get("name",    "").strip() or None
    telefon = body.get("telefon", "").strip() or None
    with db_conn() as conn:
        row = conn.execute("SELECT id FROM vk_users WHERE id = ?", (user_id,)).fetchone()
        if not row:
            return {"error": "Nicht gefunden"}, 404
        conn.execute(
            "UPDATE vk_users SET name = ?, telefon = ? WHERE id = ?",
            (name, telefon, user_id),
        )
    return {"ok": True}


@auth_bp.route("/api/admin/verein/<int:verein_id>", methods=["PATCH"])
def admin_update_verein(verein_id: int):
    token = request.headers.get("X-Upload-Token", "")
    if not UPLOAD_TOKEN or token != UPLOAD_TOKEN:
        return {"error": "Unauthorized"}, 401
    body = request.get_json(silent=True) or {}
    with db_conn() as conn:
        row = conn.execute(
            "SELECT id FROM vereine_accounts WHERE id = ?", (verein_id,)
        ).fetchone()
        if not row:
            return {"error": "Nicht gefunden"}, 404
        fields = {}
        for f in ("verein_name", "rubrik", "heimatort", "plz", "gemeinde", "landkreis"):
            if f in body:
                fields[f] = (body[f] or "").strip() or None
        if "plz" in fields and fields["plz"]:
            import re as _re
            if not _re.match(r"^\d{5}$", fields["plz"]):
                fields.pop("plz")
        if fields:
            set_clause = ", ".join(f"{k} = ?" for k in fields)
            conn.execute(
                f"UPDATE vereine_accounts SET {set_clause} WHERE id = ?",
                list(fields.values()) + [verein_id],
            )
    return {"ok": True}


@auth_bp.route("/api/admin/verein/<int:verein_id>", methods=["DELETE"])
def admin_delete_verein(verein_id: int):
    token = request.headers.get("X-Upload-Token", "")
    if not UPLOAD_TOKEN or token != UPLOAD_TOKEN:
        return {"error": "Unauthorized"}, 401
    body = request.get_json(silent=True) or {}
    delete_termine = bool(body.get("delete_termine", False))
    with db_conn() as conn:
        row = conn.execute(
            "SELECT verein_key, verein_name FROM vereine_accounts WHERE id = ?",
            (verein_id,),
        ).fetchone()
        if not row:
            return {"error": "Nicht gefunden"}, 404
        verein_key  = row["verein_key"]
        verein_name = row["verein_name"]
        user_ids = [
            r["id"] for r in conn.execute(
                "SELECT id FROM vk_users WHERE verein_id = ?", (verein_id,)
            ).fetchall()
        ]
        for uid in user_ids:
            conn.execute("DELETE FROM vk_sessions WHERE user_id = ?", (uid,))
            conn.execute("DELETE FROM vk_audit WHERE user_id = ?", (uid,))
        conn.execute("DELETE FROM vk_users WHERE verein_id = ?", (verein_id,))
        conn.execute("DELETE FROM upload_quota WHERE verein_id = ?", (verein_id,))
        if verein_key:
            conn.execute(
                "DELETE FROM tg_subscriptions WHERE verein_key = ?", (verein_key,)
            )
        conn.execute("DELETE FROM vereine_accounts WHERE id = ?", (verein_id,))
    geloescht_termine = 0
    if delete_termine and verein_key:
        try:
            from shared.kalender_store import KalenderStore
            def _rm(data):
                nonlocal geloescht_termine
                geloescht_termine = len(data.pop(verein_key, []))
                data.get("_labels", {}).pop(verein_key, None)
                data.get("_meta",   {}).pop(verein_key, None)
            KalenderStore.update(_rm)
        except Exception:
            pass
    return {"ok": True, "geloescht_termine": geloescht_termine}




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
