import json
import os
import uuid
from datetime import date, datetime

from flask import Blueprint, redirect, request

from shared.kalender_store import KalenderStore
from shared.kalender_core import VEREINSTERMINE_FILE
from shared.vk_db import db_conn, get_session_user, log_audit
from services.auth.routes import _CSS, _page, _session_token, require_verein_login

verein_bp = Blueprint("verein", __name__)

_BACK_DASH = '<a class="btn btn-sec" href="/verein/dashboard" style="margin-top:.75rem">← Zurück</a>'


def _load_data() -> dict:
    return json.loads(VEREINSTERMINE_FILE.read_text())


def _get_verein_termine(verein_key: str) -> list:
    data = _load_data()
    alle = data.get(verein_key, [])
    return [t for t in alle if not t.get("deleted")]


# ── Dashboard ────────────────────────────────────────────────────────────────

@verein_bp.route("/verein/dashboard")
@require_verein_login
def dashboard(user):
    verein_key = user["verein_key"]
    verein_name = user["verein_name"]
    termine = _get_verein_termine(verein_key)
    heute = date.today().isoformat()

    rows = ""
    for t in sorted(termine, key=lambda x: x.get("datum", "")):
        past = t.get("datum", "") < heute
        style = "opacity:.5" if past else ""
        edit_btn = ""
        if user["role"] == "admin":
            edit_btn = f'<a href="/verein/termine/{t["id"]}" style="margin-left:.5rem;color:#0a84ff;text-decoration:none">✏️</a>'
        rows += f"""<div class="card" style="{style}">
  <div style="display:flex;justify-content:space-between;align-items:start">
    <div>
      <div style="font-weight:600">{t.get('bezeichnung','')}</div>
      <div style="color:#aeaeb2;font-size:.85rem">{t.get('datum','')} {t.get('uhrzeit','')}</div>
      <div style="color:#aeaeb2;font-size:.85rem">{t.get('ort','')}</div>
    </div>
    <div>{edit_btn}</div>
  </div>
</div>"""

    if not rows:
        rows = '<p style="color:#aeaeb2">Noch keine Termine eingetragen.</p>'

    neu_btn = ""
    if user["role"] == "admin":
        neu_btn = '<a class="btn" href="/verein/termine/neu">+ Neuer Termin</a>'

    mitglieder_link = ""
    if user["role"] == "admin":
        mitglieder_link = '<a class="btn btn-sec" href="/verein/mitglieder" style="margin-top:.5rem">👥 Mitglieder</a>'

    body = f"""
<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:1rem">
  <div>
    <div style="font-weight:600">{verein_name}</div>
    <div style="color:#aeaeb2;font-size:.85rem">{user['email']} · {user['role']}</div>
  </div>
  <form method="post" action="/verein/logout">
    <button class="btn btn-sec" style="width:auto;padding:.5rem .875rem;font-size:.85rem">Logout</button>
  </form>
</div>
{neu_btn}
<h2 style="font-size:1rem;margin:1rem 0 .5rem">Termine ({len(termine)})</h2>
{rows}
{mitglieder_link}
<hr>
<a class="btn btn-sec" href="/verein/passwort" style="margin-top:.5rem">🔑 Passwort ändern</a>
<a href="/" style="display:block;text-align:center;color:#aeaeb2;font-size:.85rem;margin-top:1rem">← Zum Kalender</a>"""
    return _page(f"Dashboard – {verein_name}", body)


# ── Neuer Termin ─────────────────────────────────────────────────────────────

@verein_bp.route("/verein/termine/neu", methods=["GET", "POST"])
@require_verein_login
def termin_neu(user):
    if user["role"] != "admin":
        return redirect("/verein/dashboard")

    error = ""
    if request.method == "POST":
        datum = request.form.get("datum", "").strip()
        uhrzeit = request.form.get("uhrzeit", "").strip()
        bezeichnung = request.form.get("bezeichnung", "").strip()
        ort = request.form.get("ort", "").strip()

        if not datum or not bezeichnung:
            error = "Datum und Bezeichnung sind Pflichtfelder."
        else:
            termin_id = str(uuid.uuid4())[:8]
            neuer_termin = {
                "id": termin_id,
                "datum": datum,
                "uhrzeit": uhrzeit,
                "bezeichnung": bezeichnung,
                "ort": ort,
                "erstellt_von": user["email"],
                "erstellt_am": datetime.utcnow().isoformat()[:19],
            }
            verein_key = user["verein_key"]

            def updater(data):
                if verein_key not in data:
                    data[verein_key] = []
                    data["_labels"] = data.get("_labels", {})
                    data["_labels"][verein_key] = user["verein_name"]
                data[verein_key].append(neuer_termin)
                return data

            KalenderStore.update(updater)
            log_audit("erstellt", termin_id, verein_key, user["id"])
            return redirect("/verein/dashboard")

    form = f"""
{'<p class="err">'+error+'</p>' if error else ''}
<form method="post" autocomplete="off">
  <label>Datum *</label>
  <input name="datum" type="date" required value="{date.today().isoformat()}">
  <label>Uhrzeit</label>
  <input name="uhrzeit" type="time" placeholder="optional">
  <label>Bezeichnung *</label>
  <input name="bezeichnung" type="text" required placeholder="z.B. Jahreshauptversammlung">
  <label>Ort / Veranstaltungsort</label>
  <input name="ort" type="text" placeholder="z.B. Gasthaus zur Post">
  <button class="btn" type="submit">Termin speichern</button>
</form>
{_BACK_DASH}"""
    return _page("Neuer Termin", form)


# ── Termin bearbeiten / löschen ───────────────────────────────────────────────

@verein_bp.route("/verein/termine/<termin_id>", methods=["GET", "POST"])
@require_verein_login
def termin_edit(user, termin_id):
    if user["role"] != "admin":
        return redirect("/verein/dashboard")

    verein_key = user["verein_key"]
    data = _load_data()
    termine_list = data.get(verein_key, [])
    termin = next((t for t in termine_list if t.get("id") == termin_id and not t.get("deleted")), None)

    if not termin:
        return redirect("/verein/dashboard")

    if request.method == "POST":
        aktion = request.form.get("aktion", "")
        if aktion == "loeschen":
            def del_updater(d):
                for t in d.get(verein_key, []):
                    if t.get("id") == termin_id:
                        t["deleted"] = True
                        t["geloescht_von"] = user["email"]
                        t["geloescht_am"] = datetime.utcnow().isoformat()[:19]
                return d
            KalenderStore.update(del_updater)
            log_audit("geloescht", termin_id, verein_key, user["id"])
            return redirect("/verein/dashboard")
        else:
            datum = request.form.get("datum", "").strip()
            uhrzeit = request.form.get("uhrzeit", "").strip()
            bezeichnung = request.form.get("bezeichnung", "").strip()
            ort = request.form.get("ort", "").strip()
            if datum and bezeichnung:
                def edit_updater(d):
                    for t in d.get(verein_key, []):
                        if t.get("id") == termin_id:
                            t["datum"] = datum
                            t["uhrzeit"] = uhrzeit
                            t["bezeichnung"] = bezeichnung
                            t["ort"] = ort
                            t["geaendert_von"] = user["email"]
                            t["geaendert_am"] = datetime.utcnow().isoformat()[:19]
                    return d
                KalenderStore.update(edit_updater)
                log_audit("geaendert", termin_id, verein_key, user["id"])
                return redirect("/verein/dashboard")

    form = f"""
<form method="post">
  <label>Datum</label>
  <input name="datum" type="date" required value="{termin.get('datum','')}">
  <label>Uhrzeit</label>
  <input name="uhrzeit" type="time" value="{termin.get('uhrzeit','')}">
  <label>Bezeichnung</label>
  <input name="bezeichnung" type="text" required value="{termin.get('bezeichnung','')}">
  <label>Ort</label>
  <input name="ort" type="text" value="{termin.get('ort','')}">
  <button class="btn" type="submit" name="aktion" value="speichern">Änderungen speichern</button>
</form>
<hr>
<form method="post" onsubmit="return confirm('Termin wirklich löschen?')">
  <button class="btn btn-danger" type="submit" name="aktion" value="loeschen">🗑 Termin löschen</button>
</form>
{_BACK_DASH}"""
    return _page("Termin bearbeiten", form)


# ── Passwort ändern ───────────────────────────────────────────────────────────

@verein_bp.route("/verein/passwort", methods=["GET", "POST"])
@require_verein_login
def change_password(user):
    from services.auth.routes import _hash_pw, _check_pw
    error = ""
    success = ""
    if request.method == "POST":
        alt = request.form.get("password_alt", "")
        neu = request.form.get("password_neu", "")
        neu2 = request.form.get("password_neu2", "")
        with db_conn() as conn:
            row = conn.execute(
                "SELECT password_hash FROM vk_users WHERE id=?", (user["id"],)
            ).fetchone()
            if not _check_pw(alt, row["password_hash"]):
                error = "Aktuelles Passwort falsch."
            elif len(neu) < 8:
                error = "Neues Passwort muss mindestens 8 Zeichen haben."
            elif neu != neu2:
                error = "Neue Passwörter stimmen nicht überein."
            else:
                conn.execute(
                    "UPDATE vk_users SET password_hash=? WHERE id=?",
                    (_hash_pw(neu), user["id"]),
                )
                success = "Passwort erfolgreich geändert."

    form = f"""
{'<p class="err">'+error+'</p>' if error else ''}
{'<p class="ok">'+success+'</p>' if success else ''}
<form method="post" autocomplete="off">
  <label>Aktuelles Passwort</label>
  <input name="password_alt" type="password" required autocomplete="current-password">
  <label>Neues Passwort</label>
  <input name="password_neu" type="password" required autocomplete="new-password">
  <label>Neues Passwort wiederholen</label>
  <input name="password_neu2" type="password" required autocomplete="new-password">
  <button class="btn" type="submit">Passwort ändern</button>
</form>
{_BACK_DASH}"""
    return _page("Passwort ändern", form)


# ── Mitglieder ────────────────────────────────────────────────────────────────

@verein_bp.route("/verein/mitglieder", methods=["GET", "POST"])
@require_verein_login
def mitglieder(user):
    if user["role"] != "admin":
        return redirect("/verein/dashboard")

    import secrets as _sec
    from shared.vk_mail import send_invite_email

    error = ""
    success = ""
    if request.method == "POST":
        aktion = request.form.get("aktion", "")
        if aktion == "einladen":
            email = request.form.get("email", "").strip().lower()
            with db_conn() as conn:
                count = conn.execute(
                    "SELECT COUNT(*) FROM vk_users WHERE verein_id=?", (user["verein_id"],)
                ).fetchone()[0]
                if count >= 3:
                    error = "Maximal 3 Accounts pro Verein (Admin + 2 Mitglieder)."
                else:
                    ex = conn.execute(
                        "SELECT id FROM vk_users WHERE email=?", (email,)
                    ).fetchone()
                    if ex:
                        error = "Diese E-Mail ist bereits registriert."
                    else:
                        import bcrypt as _bc
                        token = _sec.token_urlsafe(32)
                        expires = (datetime.utcnow() + timedelta(hours=48)).isoformat()
                        tmp_hash = _bc.hashpw(_sec.token_hex(16).encode(), _bc.gensalt()).decode()
                        conn.execute(
                            """INSERT INTO vk_users
                               (email, password_hash, verein_id, role,
                                einladungs_token, einladungs_expires, email_verified, aktiv)
                               VALUES (?,?,?,'member',?,?,1,0)""",
                            (email, tmp_hash, user["verein_id"], token, expires),
                        )
                        send_invite_email(email, token, user["verein_name"])
                        success = f"Einladung an {email} verschickt."
        elif aktion == "entfernen":
            member_id = int(request.form.get("member_id", 0))
            with db_conn() as conn:
                conn.execute(
                    "DELETE FROM vk_users WHERE id=? AND verein_id=? AND role='member'",
                    (member_id, user["verein_id"]),
                )
                success = "Mitglied entfernt."

    with db_conn() as conn:
        members = conn.execute(
            "SELECT id, email, role, aktiv, email_verified FROM vk_users WHERE verein_id=? ORDER BY id",
            (user["verein_id"],),
        ).fetchall()

    rows = ""
    for m in members:
        status = "✅" if m["aktiv"] else "⏳ Einladung ausstehend"
        remove_btn = ""
        if m["role"] == "member":
            remove_btn = f'<form method="post" style="display:inline"><input type="hidden" name="aktion" value="entfernen"><input type="hidden" name="member_id" value="{m["id"]}"><button style="background:none;border:none;color:#ff453a;cursor:pointer;font-size:.9rem" type="submit">Entfernen</button></form>'
        rows += f'<div class="card"><div style="display:flex;justify-content:space-between"><div><div>{m["email"]}</div><div style="color:#aeaeb2;font-size:.82rem">{m["role"]} · {status}</div></div><div>{remove_btn}</div></div></div>'

    invite_form = ""
    if len(members) < 3:
        invite_form = f"""
{'<p class="err">'+error+'</p>' if error else ''}
{'<p class="ok">'+success+'</p>' if success else ''}
<form method="post">
  <input type="hidden" name="aktion" value="einladen">
  <label>E-Mail-Adresse des neuen Mitglieds</label>
  <input name="email" type="email" required placeholder="mitglied@beispiel.de">
  <button class="btn" type="submit">Einladung verschicken</button>
</form>
<div class="spam-hint">📬 Bitte Eingeladene auf den Spam-Ordner hinweisen.</div>"""
    else:
        invite_form = '<p class="hint">Maximale Anzahl (3) erreicht.</p>'
        if success:
            invite_form = f'<p class="ok">{success}</p>' + invite_form

    body = f"""
<h2 style="font-size:1rem;margin:0 0 .5rem">Mitglieder ({len(members)}/3)</h2>
{rows}
<hr>
{invite_form}
{_BACK_DASH}"""
    return _page("Mitglieder", body)


# ── Einladung annehmen ────────────────────────────────────────────────────────

@verein_bp.route("/verein/einladung", methods=["GET", "POST"])
def einladung():
    from services.auth.routes import _hash_pw
    token = request.args.get("token", "") or request.form.get("token", "")
    error = ""
    with db_conn() as conn:
        row = conn.execute(
            "SELECT id, einladungs_expires, aktiv FROM vk_users WHERE einladungs_token=?",
            (token,),
        ).fetchone()
        if not row:
            return _page("Ungültig", '<p class="err">Ungültiger Einladungslink.</p>'), 400
        if datetime.fromisoformat(row["einladungs_expires"]) < datetime.utcnow():
            return _page("Abgelaufen", '<p class="err">Der Einladungslink ist abgelaufen. Bitte den Vereinsadmin um eine neue Einladung bitten.</p>'), 400
        if row["aktiv"]:
            return redirect("/verein/login")

        if request.method == "POST":
            pw = request.form.get("password", "")
            pw2 = request.form.get("password2", "")
            if len(pw) < 8:
                error = "Passwort muss mindestens 8 Zeichen haben."
            elif pw != pw2:
                error = "Passwörter stimmen nicht überein."
            else:
                conn.execute(
                    """UPDATE vk_users SET password_hash=?, aktiv=1,
                       einladungs_token=NULL, einladungs_expires=NULL WHERE id=?""",
                    (_hash_pw(pw), row["id"]),
                )
                return redirect("/verein/login")

    form = f"""
<p>Lege dein Passwort fest, um die Einladung anzunehmen.</p>
{'<p class="err">'+error+'</p>' if error else ''}
<form method="post">
  <input type="hidden" name="token" value="{token}">
  <label>Passwort <span class="hint">(mind. 8 Zeichen)</span></label>
  <input name="password" type="password" required autocomplete="new-password">
  <label>Passwort wiederholen</label>
  <input name="password2" type="password" required autocomplete="new-password">
  <button class="btn" type="submit">Einladung annehmen</button>
</form>"""
    return _page("Einladung annehmen", form)


# ── Datenschutz / Nutzungsbedingungen ────────────────────────────────────────

@verein_bp.route("/verein/datenschutz")
def datenschutz():
    body = """
<p style="color:#aeaeb2;font-size:.85rem">Stand: Mai 2026</p>
<div class="card">
<h2 style="font-size:1rem;margin-top:0">1. Verantwortlicher</h2>
<p>Josef Fischer, [Adresse], josef.jf.fischer@me.com</p>
</div>
<div class="card">
<h2 style="font-size:1rem;margin-top:0">2. Erhobene Daten</h2>
<p>Bei der Registrierung erfassen wir: E-Mail-Adresse, Vereinsname sowie das Passwort (verschlüsselt gespeichert, niemals im Klartext). Vereinstermine werden öffentlich angezeigt.</p>
</div>
<div class="card">
<h2 style="font-size:1rem;margin-top:0">3. Zweck der Verarbeitung</h2>
<p>Die Daten dienen ausschließlich dem Betrieb des Vereinskalenders: Identifizierung des Vereins, Authentifizierung des Accounts und Benachrichtigungen (Bestätigungs-E-Mails).</p>
</div>
<div class="card">
<h2 style="font-size:1rem;margin-top:0">4. Speicherdauer</h2>
<p>Accounts werden auf Anfrage oder bei dauerhafter Inaktivität (> 2 Jahre) gelöscht. Eingetragene Termine werden nach Ende des jeweiligen Kalenderjahres bereinigt.</p>
</div>
<div class="card">
<h2 style="font-size:1rem;margin-top:0">5. E-Mail-Dienst</h2>
<p>E-Mails werden über Brevo (Sendinblue SAS, Frankreich) versendet. Dabei wird die Ziel-E-Mail-Adresse an Brevo übermittelt.</p>
</div>
<div class="card">
<h2 style="font-size:1rem;margin-top:0">6. Rechte</h2>
<p>Auskunft, Berichtigung, Löschung deiner Daten: Schreib an josef.jf.fischer@me.com. Beschwerderecht bei der zuständigen Datenschutz-Aufsichtsbehörde.</p>
</div>
<a href="/verein/register" style="color:#aeaeb2;font-size:.85rem">← Zurück</a>"""
    return _page("Datenschutzerklärung", body)


@verein_bp.route("/verein/nutzungsbedingungen")
def nutzungsbedingungen():
    body = """
<p style="color:#aeaeb2;font-size:.85rem">Stand: Mai 2026</p>
<div class="card">
<h2 style="font-size:1rem;margin-top:0">1. Nutzung</h2>
<p>Der Vereinskalender vereinskalender.online dient der nicht-kommerziellen Veröffentlichung von Vereinsterminen in der Region Postau/Bayerbach. Die Nutzung ist kostenlos.</p>
</div>
<div class="card">
<h2 style="font-size:1rem;margin-top:0">2. Registrierung</h2>
<p>Nur Vereine im Einzugsgebiet dürfen sich registrieren. Gewerbliche oder kommerzielle Anbieter sind ausgeschlossen. Jeder Verein trägt Verantwortung für die Richtigkeit seiner Daten.</p>
</div>
<div class="card">
<h2 style="font-size:1rem;margin-top:0">3. Haftungsausschluss</h2>
<p>Der Betreiber übernimmt keine Gewähr für die Richtigkeit der eingetragenen Termine. Urheberrechtlich geschützte Inhalte dürfen nicht ohne Genehmigung eingestellt werden.</p>
</div>
<div class="card">
<h2 style="font-size:1rem;margin-top:0">4. Kündigung</h2>
<p>Der Betreiber kann Accounts bei Verstoß gegen diese Bedingungen ohne Vorankündigung sperren oder löschen.</p>
</div>
<a href="/verein/register" style="color:#aeaeb2;font-size:.85rem">← Zurück</a>"""
    return _page("Nutzungsbedingungen", body)
