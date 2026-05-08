import os
import smtplib
import sys
import uuid
from email import utils as email_utils
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

SMTP_HOST = "smtp-relay.brevo.com"
SMTP_PORT = 587
FROM_EMAIL = "noreply@vereinskalender.online"
FROM_NAME  = "Vereinskalender"
BASE_URL   = "https://vereinskalender.online"

_STYLE = """
<style>
  body{margin:0;padding:0;background:#f2f2f7;font-family:-apple-system,Helvetica,Arial,sans-serif}
  .wrap{padding:32px 16px}
  .card{background:#fff;border-radius:14px;padding:32px;max-width:480px;margin:0 auto;
        box-shadow:0 2px 12px rgba(0,0,0,.08)}
  .hdr{background:#6D28D9;border-radius:10px 10px 0 0;margin:-32px -32px 24px;
       padding:20px 32px;color:#fff}
  .hdr h1{margin:0;font-size:18px;font-weight:700}
  .hdr p{margin:4px 0 0;font-size:12px;opacity:.75}
  h2{color:#1c1c1e;margin:0 0 12px;font-size:17px}
  p{color:#3c3c43;line-height:1.6;margin:0 0 12px}
  .btn{display:inline-block;background:#6D28D9;color:#fff !important;text-decoration:none;
       padding:13px 28px;border-radius:10px;font-weight:600;font-size:15px;margin:8px 0 16px}
  .hint{color:#8e8e93;font-size:.82rem;line-height:1.5}
  .footer{text-align:center;margin-top:20px;color:#aeaeb2;font-size:.78rem}
</style>
"""

def _html_wrap(title: str, body: str) -> str:
    return f"""<!DOCTYPE html><html lang="de"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title}</title>{_STYLE}</head>
<body><div class="wrap"><div class="card">
<div class="hdr"><h1>Vereinskalender</h1><p>Veranstaltungen in der Region</p></div>
{body}
</div>
<p class="footer">Vereinskalender &middot; vereinskalender.online<br>
Du erhältst diese E-Mail, weil eine Aktion auf unserem Portal durchgeführt wurde.</p>
</div></body></html>"""


def _send(to_email: str, subject: str, html_body: str) -> bool:
    smtp_user = os.environ.get("BREVO_SMTP_USER", "")
    smtp_key  = os.environ.get("BREVO_SMTP_KEY", "")
    if not smtp_user or not smtp_key:
        return False
    msg = MIMEMultipart("alternative")
    msg["Subject"]      = subject
    msg["From"]         = f"{FROM_NAME} <{FROM_EMAIL}>"
    msg["To"]           = to_email
    msg["Reply-To"]     = FROM_EMAIL
    msg["Message-ID"]   = f"<{uuid.uuid4()}@vereinskalender.online>"
    msg["Date"]         = email_utils.formatdate(localtime=False)
    msg["MIME-Version"] = "1.0"
    msg["Precedence"]   = "transactional"
    msg.attach(MIMEText(html_body, "html", "utf-8"))
    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=10) as smtp:
            smtp.ehlo()
            smtp.starttls()
            smtp.ehlo()
            smtp.login(smtp_user, smtp_key)
            smtp.sendmail(FROM_EMAIL, to_email, msg.as_string())
        return True
    except Exception as e:
        print(f"MAIL ERROR to {to_email}: {e}", file=sys.stderr)
        return False


def send_verify_email(to_email: str, token: str) -> bool:
    link = f"{BASE_URL}/api/auth/verify?token={token}"
    body = f"""<h2>E-Mail-Adresse bestätigen</h2>
<p>Bitte bestätige deine E-Mail-Adresse, um deine Registrierung abzuschließen.</p>
<a class="btn" href="{link}">E-Mail bestätigen</a>
<p class="hint">Der Link ist <strong>24 Stunden</strong> gültig.<br>
Falls du dich nicht registriert hast, kannst du diese E-Mail ignorieren.<br><br>
Direktlink: <a href="{link}" style="color:#6D28D9">{link}</a></p>"""
    return _send(to_email, "Deine E-Mail-Adresse bestätigen – Vereinskalender", _html_wrap("E-Mail bestätigen", body))


def send_reset_email(to_email: str, token: str) -> bool:
    link = f"{BASE_URL}/verein/passwort-reset?token={token}"
    body = f"""<h2>Passwort zurücksetzen</h2>
<p>Du hast eine Passwort-Zurücksetzung angefordert.</p>
<a class="btn" href="{link}">Neues Passwort setzen</a>
<p class="hint">Der Link ist <strong>1 Stunde</strong> gültig.<br>
Falls du keine Zurücksetzung angefordert hast, ignoriere diese E-Mail.</p>"""
    return _send(to_email, "Passwort zurücksetzen – Vereinskalender", _html_wrap("Passwort zurücksetzen", body))


def send_invite_email(to_email: str, token: str, verein_name: str) -> bool:
    link = f"{BASE_URL}/verein/einladung?token={token}"
    body = f"""<h2>Einladung zur Mitarbeit</h2>
<p>Du wurdest eingeladen, den Vereinskalender für <strong>{verein_name}</strong> mitzuverwalten.</p>
<a class="btn" href="{link}">Einladung annehmen</a>
<p class="hint">Der Link ist <strong>48 Stunden</strong> gültig.</p>"""
    return _send(to_email, f"Einladung: {verein_name} – Vereinskalender", _html_wrap("Einladung", body))


def send_welcome_email(to_email: str, verein_name: str) -> bool:
    login_link  = f"{BASE_URL}/verein/login"
    upload_link = f"{BASE_URL}/verein/upload"
    profil_link = f"{BASE_URL}/verein/profil"
    body = f"""<h2>Willkommen beim Vereinskalender!</h2>
<p>Das Konto für <strong>{verein_name}</strong> ist freigeschaltet. In drei Schritten seid ihr dabei:</p>
<ol style="margin:12px 0 16px;padding-left:20px;color:#3c3c43;line-height:2;font-size:14px">
  <li><strong>Profil prüfen</strong> – Heimatort, PLZ und Rubrik kontrollieren:<br>
      <a href="{profil_link}" style="color:#6D28D9">{profil_link}</a></li>
  <li><strong>Termine hochladen</strong> – Jahresprogramm als PDF, Foto oder Excel:<br>
      <a href="{upload_link}" style="color:#6D28D9">{upload_link}</a></li>
  <li><strong>Kalender abonnieren</strong> – Auf <a href="{BASE_URL}" style="color:#6D28D9">vereinskalender.online</a>
      den Button <em>„Abonnieren"</em> antippen – dann habt ihr alle Termine automatisch im iPhone-Kalender.</li>
</ol>
<a class="btn" href="{login_link}">Jetzt einloggen</a>
<p class="hint">Bei Fragen einfach auf diese E-Mail antworten oder schreiben an
<a href="mailto:Vereinskalender@icloud.com" style="color:#6D28D9">Vereinskalender@icloud.com</a>.</p>"""
    return _send(to_email, f"Konto freigeschaltet – {verein_name}", _html_wrap("Willkommen!", body))


def send_rejected_email(to_email: str, verein_name: str) -> bool:
    body = f"""<h2>Registrierung nicht angenommen</h2>
<p>Die Registrierungsanfrage für <strong>{verein_name}</strong> konnte leider nicht bestätigt werden.</p>
<p>Bei Fragen wende dich direkt an den Kalender-Administrator.</p>"""
    return _send(to_email, f"Registrierungsanfrage – Vereinskalender", _html_wrap("Registrierung", body))
