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
MMDB_PATH  = Path("/opt/rename-webhook/GeoLite2-City.mmdb")
MONTHS_MAP = {"Jan":1,"Feb":2,"Mar":3,"Apr":4,"May":5,"Jun":6,
              "Jul":7,"Aug":8,"Sep":9,"Oct":10,"Nov":11,"Dec":12}

# Substrings im User-Agent (lowercase) → Zeile wird als Crawler übersprungen
CRAWLER_UA = {
    "bot", "crawler", "spider", "slurp", "scraper",
    "censys", "shodan", "masscan", "zgrab", "nmap",
    "python-requests", "python-urllib", "curl/", "wget/", "axios",
    "go-http-client", "java/", "okhttp", "httpx", "aiohttp",
    "headlesschrome", "headless chrome", "headlessfirefox", "phantomjs",
    "palo alto", "paloalto", "hello from",
    "pingdom", "uptimerobot", "statuscake", "datadog-agent",
    "facebookexternalhit", "yandex", "baiduspider", "duckduckbot",
    "petalbot", "seznambot", "msnbot", "applebot",
    "semrushbot", "ahrefsbot", "dotbot", "mj12bot", "blexbot",
    "dataforseobot", "seokicks", "serpstatbot",
    "gptbot", "claudebot", "anthropic-ai", "openai",
    "netcraft", "internet-measurement", "netsystemsresearch",
    "expanse", "intrinsec", "censysbot",
}


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
    m = re.search(r'\[(\d{2})/(\w{3})/(\d{4}):(\d{2}):(\d{2}):(\d{2}) ([+-]\d{4})\]', line)
    if not m:
        return None
    d, mo, y, h, mi, s, tz_str = m.groups()
    try:
        sign = 1 if tz_str[0] == '+' else -1
        off = timezone(timedelta(hours=sign * int(tz_str[1:3]), minutes=sign * int(tz_str[3:5])))
        return datetime(int(y), MONTHS_MAP[mo], int(d), int(h), int(mi), int(s), tzinfo=off)
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


def _geo_lookup(reader, raw_ip: str) -> tuple[str, str]:
    try:
        r = reader.city(raw_ip)
        land  = r.country.names.get("de") or r.country.name or "Unbekannt"
        stadt = (r.city.names.get("de") or r.city.name or "") if r.country.iso_code == "DE" else ""
        return land, stadt
    except Exception:
        return "Unbekannt", ""


def collect_day(target: date, max_files: int = 60) -> tuple[int, int, dict[int, int], dict[tuple[str, str], int]]:
    """Liest nginx-Logs, zählt Views + unique Besucher + stündliche Views + Geo für einen Tag."""
    v = 0
    ips: set[str] = set()
    hourly: dict[int, int] = {}
    geo_ips: dict[tuple[str, str], set[str]] = {}
    ip_pat = re.compile(r'^(\S+)')

    reader = None
    try:
        if MMDB_PATH.exists():
            import geoip2.database
            reader = geoip2.database.Reader(str(MMDB_PATH))
    except Exception:
        pass

    try:
        for log_file in _log_files(max_files):
            for line in _read_lines(log_file):
                if '"GET /kalender' not in line and '"GET / ' not in line:
                    continue
                # User-Agent extrahieren und Crawler filtern
                parts = line.split('"')
                ua = parts[-2].lower() if len(parts) >= 2 else ""
                if any(kw in ua for kw in CRAWLER_UA):
                    continue
                dt = _parse_dt(line)
                if dt is None:
                    continue
                dt_berlin = dt.astimezone(BERLIN)
                if dt_berlin.date() != target:
                    continue
                m = ip_pat.match(line)
                raw_ip = m.group(1) if m else None
                anon   = _anon_ip(raw_ip) if raw_ip else "unknown"
                ips.add(anon)
                v += 1
                hourly[dt_berlin.hour] = hourly.get(dt_berlin.hour, 0) + 1
                if reader and raw_ip:
                    geo_key = _geo_lookup(reader, raw_ip)
                    geo_ips.setdefault(geo_key, set()).add(anon)
    finally:
        if reader:
            reader.close()

    geo_counts = {k: len(v) for k, v in geo_ips.items()}
    return v, len(ips), hourly, geo_counts


def save_day(target: date, views: int, unique: int, hourly: dict[int, int],
             geo: dict[tuple[str, str], int] | None = None):
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
        if geo:
            conn.execute("DELETE FROM page_stats_geo WHERE datum=?", (target.isoformat(),))
            for (land, stadt), besucher in geo.items():
                conn.execute(
                    "INSERT INTO page_stats_geo (datum, land, stadt, besucher) VALUES (?,?,?,?)",
                    (target.isoformat(), land, stadt, besucher),
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
        views, unique, hourly, geo = collect_day(target, max_files=min(n + 10, 400))
        save_day(target, views, unique, hourly, geo)
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        print(f"{ts} [{target}] views={views} unique={unique} stunden={len(hourly)} geo={len(geo)}", flush=True)


if __name__ == "__main__":
    main()
