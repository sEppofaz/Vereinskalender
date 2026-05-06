#!/opt/rename-webhook/bin/python3
"""
kalender_report.py – Vereinskalender-Bericht per Telegram
Cron: täglich 06:00, 13:00, 20:00 CEST (= 04:00, 11:00, 18:00 UTC)
"""

import gzip
import ipaddress
import json
import re
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path
from datetime import timezone as _tz
from zoneinfo import ZoneInfo

SECRETS_FILE        = Path("/etc/pka/secrets.env")
VEREINSTERMINE_FILE = Path("/opt/rename-webhook/vereinstermine.json")
LAST_IMPORT_FILE    = Path("/opt/rename-webhook/last_import.json")
NGINX_LOG           = Path("/var/log/nginx/vereinskalender.access.log")
TZ_LOCAL            = ZoneInfo("Europe/Berlin")

_MONTHS = {"Jan":1,"Feb":2,"Mar":3,"Apr":4,"May":5,"Jun":6,
           "Jul":7,"Aug":8,"Sep":9,"Oct":10,"Nov":11,"Dec":12}


def load_secrets() -> dict:
    secrets = {}
    for line in SECRETS_FILE.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        line = re.sub(r"^export\s+", "", line)
        if "=" in line:
            k, _, v = line.partition("=")
            secrets[k.strip()] = v.strip().strip('"').strip("'")
    return secrets


def send_telegram(token: str, chat_id: str, text: str) -> None:
    payload = json.dumps({"chat_id": chat_id, "text": text, "parse_mode": "HTML"}).encode()
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data=payload,
        headers={"Content-Type": "application/json"},
    )
    urllib.request.urlopen(req, timeout=10)


def parse_log_dt(line: str) -> datetime | None:
    pat = re.compile(r'\[(\d{2})/(\w{3})/(\d{4}):(\d{2}):(\d{2}):(\d{2})')
    m = pat.search(line)
    if not m:
        return None
    d, mo, y, h, mi, s = m.groups()
    try:
        return datetime(int(y), _MONTHS[mo], int(d), int(h), int(mi), int(s))
    except (KeyError, ValueError):
        return None


def _log_files_last_n_days(n: int) -> list[Path]:
    """Gibt aktuelle + rotierte nginx-Log-Dateien der letzten n Tage zurück."""
    files = []
    name = NGINX_LOG.name  # z.B. "vereinskalender.access.log"
    if NGINX_LOG.exists():
        files.append(NGINX_LOG)
    for i in range(1, n + 1):
        plain = NGINX_LOG.parent / f"{name}.{i}"
        gz    = NGINX_LOG.parent / f"{name}.{i}.gz"
        if plain.exists():
            files.append(plain)
        elif gz.exists():
            files.append(gz)
    return files


def _read_lines(path: Path) -> list[str]:
    try:
        if path.suffix == ".gz":
            with gzip.open(path, "rt", errors="ignore") as f:
                return f.readlines()
        return path.read_text(errors="ignore").splitlines()
    except Exception:
        return []


def _anonymize_ip(raw: str) -> str:
    """IPv4 → /24 (letztes Oktet 0), IPv6 → /48 (letzte 80 Bit 0)."""
    try:
        addr = ipaddress.ip_address(raw)
        if isinstance(addr, ipaddress.IPv4Address):
            return str(ipaddress.ip_network(f"{raw}/24", strict=False).network_address)
        return str(ipaddress.ip_network(f"{raw}/48", strict=False).network_address)
    except ValueError:
        return "unknown"


def count_page_views() -> tuple[int, int, int, int]:
    """Gibt (aufrufe_heute, aufrufe_7d, unique_heute, unique_7d) zurück."""
    now    = datetime.now(_tz.utc).replace(tzinfo=None)
    heute  = now.replace(hour=0, minute=0, second=0, microsecond=0)
    cutoff = now - timedelta(days=7)
    heute_count  = 0
    week_count   = 0
    heute_ips: set[str] = set()
    week_ips:  set[str] = set()
    ip_pat = re.compile(r'^(\S+)')
    for log_file in _log_files_last_n_days(7):
        for line in _read_lines(log_file):
            if '"GET /kalender' not in line and '"GET / ' not in line:
                continue
            dt = parse_log_dt(line)
            if dt is None:
                continue
            m = ip_pat.match(line)
            anon_ip = _anonymize_ip(m.group(1)) if m else "unknown"
            if dt >= heute:
                heute_count += 1
                heute_ips.add(anon_ip)
            if dt >= cutoff:
                week_count += 1
                week_ips.add(anon_ip)
    return heute_count, week_count, len(heute_ips), len(week_ips)


def verein_stats() -> tuple[int, int]:
    if not VEREINSTERMINE_FILE.exists():
        return 0, 0
    try:
        data = json.loads(VEREINSTERMINE_FILE.read_text())
    except Exception:
        return 0, 0
    heute = datetime.now().strftime("%Y-%m-%d")
    gesamt = 0
    aktiv  = 0
    for key, items in data.items():
        if key.startswith("_") or not isinstance(items, list):
            continue
        gesamt += 1
        if any(t.get("datum", "") >= heute for t in items):
            aktiv += 1
    return gesamt, aktiv


def last_import_info() -> str:
    if not LAST_IMPORT_FILE.exists():
        return "–"
    try:
        li = json.loads(LAST_IMPORT_FILE.read_text())
        dt = datetime.strptime(li["datum"], "%Y-%m-%d %H:%M")
        return f"{dt.strftime('%d.%m.%Y, %H:%M')} ({li['termine']} Termine, {li['vereine']} Vereine)"
    except Exception:
        return "–"


def main():
    secrets = load_secrets()
    heute_views, week_views, heute_unique, week_unique = count_page_views()
    gesamt, aktiv  = verein_stats()
    letzter_import = last_import_info()
    jetzt          = datetime.now(TZ_LOCAL).strftime("%d.%m.%Y, %H:%M")

    text = (
        f"📊 <b>Vereinskalender</b> · {jetzt}\n\n"
        f"🌐 Aufrufe heute: <b>{heute_views}</b>  |  7 Tage: <b>{week_views}</b>\n"
        f"👤 Besucher heute: <b>{heute_unique}</b>  |  7 Tage: <b>{week_unique}</b>\n"
        f"🏛 Vereine: <b>{gesamt} gesamt</b>, davon <b>{aktiv} mit künftigen Terminen</b>\n"
        f"📥 Letzter Import: <b>{letzter_import}</b>"
    )

    send_telegram(secrets["TOKEN"], secrets["CHAT_ID"], text)
    print(f"✅ Bericht gesendet ({jetzt})")


if __name__ == "__main__":
    main()
