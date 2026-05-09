#!/usr/bin/env python3
"""
Täglicher Besucherstatistik-Sammler für Vereinskalender.

Crontab (täglich 00:05 Uhr):
  5 0 * * * /opt/rename-webhook/bin/python3 /opt/rename-webhook/stats_collector.py >> /var/log/pka-stats.log 2>&1

Erster Lauf mit Backfill (z.B. letzte 60 Tage aus vorhandenen Logs):
  python3 stats_collector.py --backfill 60
"""
import argparse
import gzip
import ipaddress
import re
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).parent))
from shared.vk_db import db_conn

BERLIN     = ZoneInfo("Europe/Berlin")
NGINX_LOG  = Path("/var/log/nginx/vereinskalender.access.log")
MONTHS_MAP = {"Jan":1,"Feb":2,"Mar":3,"Apr":4,"May":5,"Jun":6,
              "Jul":7,"Aug":8,"Sep":9,"Oct":10,"Nov":11,"Dec":12}


def _log_files(n: int) -> list[Path]:
    files = [NGINX_LOG] if NGINX_LOG.exists() else []
    for i in range(1, n + 1):
        p  = NGINX_LOG.parent / f"{NGINX_LOG.name}.{i}"
        gz = NGINX_LOG.parent / f"{NGINX_LOG.name}.{i}.gz"
        if p.exists():    files.append(p)
        elif gz.exists(): files.append(gz)
    return files


def _read_lines(path: Path) -> list[str]:
    try:
        if path.suffix == ".gz":
            with gzip.open(path, "rt", errors="ignore") as f:
                return f.readlines()
        return path.read_text(errors="ignore").splitlines()
    except Exception:
        return []


def _parse_dt(line: str) -> datetime | None:
    m = re.search(r'\[(\d{2})/(\w{3})/(\d{4}):(\d{2}):(\d{2}):(\d{2})', line)
    if not m:
        return None
    d, mo, y, h, mi, s = m.groups()
    try:
        return datetime(int(y), MONTHS_MAP[mo], int(d), int(h), int(mi), int(s),
                        tzinfo=timezone.utc)
    except (KeyError, ValueError):
        return None


def _anon_ip(raw: str) -> str:
    try:
        addr = ipaddress.ip_address(raw)
        if isinstance(addr, ipaddress.IPv4Address):
            return str(ipaddress.ip_network(f"{raw}/24", strict=False).network_address)
        return str(ipaddress.ip_network(f"{raw}/48", strict=False).network_address)
    except ValueError:
        return "unknown"


def collect_day(target: date, max_files: int = 60) -> tuple[int, int, dict[int, int]]:
    """Liest nginx-Logs, zählt Views + unique Besucher + stündliche Views für einen Tag."""
    v = 0
    ips: set[str] = set()
    hourly: dict[int, int] = {}
    ip_pat = re.compile(r'^(\S+)')
    for log_file in _log_files(max_files):
        for line in _read_lines(log_file):
            if '"GET /kalender' not in line and '"GET / ' not in line:
                continue
            dt = _parse_dt(line)
            if dt is None:
                continue
            dt_berlin = dt.astimezone(BERLIN)
            if dt_berlin.date() != target:
                continue
            m = ip_pat.match(line)
            ips.add(_anon_ip(m.group(1)) if m else "unknown")
            v += 1
            hourly[dt_berlin.hour] = hourly.get(dt_berlin.hour, 0) + 1
    return v, len(ips), hourly


def save_day(target: date, views: int, unique: int, hourly: dict[int, int]):
    with db_conn() as conn:
        conn.execute(
            """INSERT INTO page_stats (datum, views, unique_visitors) VALUES (?,?,?)
               ON CONFLICT(datum) DO UPDATE SET
                 views=excluded.views,
                 unique_visitors=excluded.unique_visitors""",
            (target.isoformat(), views, unique),
        )
        for stunde, h_views in hourly.items():
            conn.execute(
                """INSERT INTO page_stats_hourly (datum, stunde, views) VALUES (?,?,?)
                   ON CONFLICT(datum, stunde) DO UPDATE SET views=excluded.views""",
                (target.isoformat(), stunde, h_views),
            )


def main():
    parser = argparse.ArgumentParser(description="Vereinskalender Besucherstatistik sammeln")
    parser.add_argument("--backfill", type=int, default=0,
                        help="Rückwirkend N Tage auffüllen (0 = nur gestern)")
    args = parser.parse_args()

    today = datetime.now(BERLIN).date()
    n     = max(args.backfill, 1)

    for i in range(n, 0, -1):
        target = today - timedelta(days=i)
        views, unique, hourly = collect_day(target, max_files=min(n + 10, 400))
        save_day(target, views, unique, hourly)
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        print(f"{ts} [{target}] views={views} unique={unique} stunden={len(hourly)}", flush=True)


if __name__ == "__main__":
    main()
