import gzip
import ipaddress
import json
import os
import re
import time
import uuid as _uuid
from datetime import date, datetime, timedelta, timezone as _tz
from pathlib import Path

from flask import Blueprint, Response, request

from shared.vk_db import db_conn
from shared.kalender_core import (
    GOTTESDIENSTE_FILE,
    ICON_192_FILE,
    ICON_512_FILE,
    KALENDER_HTML_FILE,
    MEDIA_TYPES,
    VEREINSTERMINE_FILE,
    _HEIC_SUPPORTED,
    _PG_KEYS,
    _PG_LABELS,
    _do_save_import,
    _make_verein_key,
    import_pdf_bytes,
    log,
    lookup_plz,
)

kalender_bp = Blueprint("kalender", __name__)

UPLOAD_TOKEN = os.environ.get("UPLOAD_TOKEN", "")


@kalender_bp.route("/manifest.json")
def manifest_json():
    return json.dumps({
        "name":             "Vereinskalender",
        "short_name":       "Vereinskalender",
        "start_url":        "/",
        "display":          "standalone",
        "background_color": "#1c1c1e",
        "theme_color":      "#1c1c1e",
        "icons": [
            {"src": "/icon-192.png", "sizes": "192x192", "type": "image/png", "purpose": "any maskable"},
            {"src": "/icon-512.png", "sizes": "512x512", "type": "image/png", "purpose": "any maskable"},
        ],
    }), 200, {"Content-Type": "application/manifest+json"}


@kalender_bp.route("/icon-192.png")
def icon_192():
    if ICON_192_FILE.exists():
        return ICON_192_FILE.read_bytes(), 200, {"Content-Type": "image/png"}
    return "", 404


@kalender_bp.route("/icon-512.png")
def icon_512():
    if ICON_512_FILE.exists():
        return ICON_512_FILE.read_bytes(), 200, {"Content-Type": "image/png"}
    return "", 404


@kalender_bp.route("/apple-touch-icon.png")
def apple_touch_icon():
    if ICON_192_FILE.exists():
        return ICON_192_FILE.read_bytes(), 200, {"Content-Type": "image/png"}
    return "", 404


@kalender_bp.route("/kalender")
def kalender_page():
    try:
        html = KALENDER_HTML_FILE.read_text(encoding="utf-8")
    except FileNotFoundError:
        html = "<h1>kalender.html nicht gefunden</h1>"
    return html, 200, {"Content-Type": "text/html; charset=utf-8", "Cache-Control": "no-store"}


@kalender_bp.route("/upload", methods=["POST"])
def upload_kalender():
    token = request.headers.get("X-Upload-Token", "")
    if not UPLOAD_TOKEN or token != UPLOAD_TOKEN:
        log("⚠️  /upload: ungültiges Token")
        return json.dumps({"error": "Nicht autorisiert"}), 401, {"Content-Type": "application/json"}
    if "file" not in request.files:
        return json.dumps({"error": "Keine Datei"}), 400, {"Content-Type": "application/json"}

    f      = request.files["file"]
    fname  = (f.filename or "").lower()
    suffix = Path(fname).suffix if fname else ""
    _KALENDER_ALLOWED = {".pdf", ".jpg", ".jpeg", ".png", ".heic", ".heif"}

    if suffix not in _KALENDER_ALLOWED:
        return json.dumps({"error": "Nur PDF oder Bilder (JPG, PNG, HEIC)"}), 400, {"Content-Type": "application/json"}
    if suffix in {".heic", ".heif"} and not _HEIC_SUPPORTED:
        return json.dumps({"error": "HEIC-Format auf diesem Server nicht verfügbar"}), 400, {"Content-Type": "application/json"}

    try:
        result   = import_pdf_bytes(f.read(), suffix)
        alle     = result["alle"]
        auto_plz = result["auto_plz"]

        try:
            data = json.loads(VEREINSTERMINE_FILE.read_text()) if VEREINSTERMINE_FILE.exists() else {}
        except Exception:
            data = {}

        ort_cfg     = data.get("_ortschaften", {"whitelist": [], "blacklist": []})
        known_white = set(ort_cfg.get("whitelist", []))
        known_black = set(ort_cfg.get("blacklist", []))
        neue_orts   = sorted({
            t.get("ortschaft", "").strip()
            for t in alle
            if t.get("ortschaft", "").strip()
               and t["ortschaft"].strip() not in known_white
               and t["ortschaft"].strip() not in known_black
        })

        known_labels = set(data.get("_labels", {}).keys())
        seen_keys: set = set()
        neue_vereine_ohne_ort: list = []
        for t in alle:
            vname = (t.get("verein") or "").strip()
            if not vname:
                continue
            vkey = _make_verein_key(vname)
            if vkey in known_labels or vkey in seen_keys:
                continue
            seen_keys.add(vkey)
            parts     = vname.strip().split()
            last_word = parts[-1].split("/")[0] if parts else ""
            if len(last_word) <= 4 or last_word not in known_white:
                neue_vereine_ohne_ort.append({"key": vkey, "name": vname})

        if neue_orts or neue_vereine_ohne_ort:
            import_id = str(_uuid.uuid4())
            form_plz  = request.form.get("plz", "").strip()
            Path(f"/tmp/vk_pending_{import_id}.json").write_text(
                json.dumps({
                    "import_id": import_id,
                    "alle":      alle,
                    "auto_plz":  auto_plz,
                    "form_plz":  form_plz,
                }, ensure_ascii=False)
            )
            log(f"⏳  Upload ausstehend: {len(neue_orts)} Ortschaften, {len(neue_vereine_ohne_ort)} Vereine ohne Heimatort")
            return (
                json.dumps({
                    "pending":                     True,
                    "import_id":                   import_id,
                    "pending_ortschaften":         neue_orts,
                    "neue_vereine_ohne_ortschaft": neue_vereine_ohne_ort,
                    "preview": {
                        "termine_count": len(alle),
                        "vereine":       sorted({t.get("verein", "") for t in alle}),
                    },
                }, ensure_ascii=False),
                200,
                {"Content-Type": "application/json; charset=utf-8"},
            )

        form_plz           = request.form.get("plz", "").strip()
        result_vereine, total = _do_save_import(alle, auto_plz, form_plz, data)
        return (
            json.dumps({"success": True, "vereine": result_vereine, "total": total}, ensure_ascii=False),
            200,
            {"Content-Type": "application/json; charset=utf-8"},
        )

    except Exception as ex:
        log(f"❌  /upload Fehler: {ex}")
        return json.dumps({"error": str(ex)}), 500, {"Content-Type": "application/json"}


@kalender_bp.route("/api/check-token", methods=["POST"])
def api_check_token():
    token = request.headers.get("X-Upload-Token", "")
    if not UPLOAD_TOKEN or token != UPLOAD_TOKEN:
        return "", 401
    return "", 200


_NGINX_LOG  = Path("/var/log/nginx/vereinskalender.access.log")
_MONTHS_MAP = {"Jan":1,"Feb":2,"Mar":3,"Apr":4,"May":5,"Jun":6,
               "Jul":7,"Aug":8,"Sep":9,"Oct":10,"Nov":11,"Dec":12}


def _stats_log_files(n: int) -> list[Path]:
    files = [_NGINX_LOG] if _NGINX_LOG.exists() else []
    for i in range(1, n + 1):
        p  = _NGINX_LOG.parent / f"{_NGINX_LOG.name}.{i}"
        gz = _NGINX_LOG.parent / f"{_NGINX_LOG.name}.{i}.gz"
        if p.exists():   files.append(p)
        elif gz.exists(): files.append(gz)
    return files


def _stats_read_lines(path: Path) -> list[str]:
    try:
        if path.suffix == ".gz":
            with gzip.open(path, "rt", errors="ignore") as f:
                return f.readlines()
        return path.read_text(errors="ignore").splitlines()
    except Exception:
        return []


def _stats_parse_dt(line: str) -> datetime | None:
    m = re.search(r'\[(\d{2})/(\w{3})/(\d{4}):(\d{2}):(\d{2}):(\d{2})', line)
    if not m:
        return None
    d, mo, y, h, mi, s = m.groups()
    try:
        return datetime(int(y), _MONTHS_MAP[mo], int(d), int(h), int(mi), int(s))
    except (KeyError, ValueError):
        return None


def _stats_anon_ip(raw: str) -> str:
    try:
        addr = ipaddress.ip_address(raw)
        if isinstance(addr, ipaddress.IPv4Address):
            return str(ipaddress.ip_network(f"{raw}/24", strict=False).network_address)
        return str(ipaddress.ip_network(f"{raw}/48", strict=False).network_address)
    except ValueError:
        return "unknown"


def _count_page_views() -> tuple[int, int, int, int]:
    now    = datetime.now(_tz.utc).replace(tzinfo=None)
    heute  = now.replace(hour=0, minute=0, second=0, microsecond=0)
    cutoff = now - timedelta(days=7)
    h_cnt = w_cnt = 0
    h_ips: set[str] = set()
    w_ips: set[str] = set()
    ip_pat = re.compile(r'^(\S+)')
    for log_file in _stats_log_files(7):
        for line in _stats_read_lines(log_file):
            if '"GET /kalender' not in line and '"GET / ' not in line:
                continue
            dt = _stats_parse_dt(line)
            if dt is None:
                continue
            m = ip_pat.match(line)
            anon = _stats_anon_ip(m.group(1)) if m else "unknown"
            if dt >= heute:
                h_cnt += 1; h_ips.add(anon)
            if dt >= cutoff:
                w_cnt += 1; w_ips.add(anon)
    return h_cnt, w_cnt, len(h_ips), len(w_ips)


def _count_today_live() -> tuple[int, int]:
    """Liest die aktuellen heutigen Aufrufe live aus dem nginx-Log (Europe/Berlin)."""
    from zoneinfo import ZoneInfo
    berlin = ZoneInfo("Europe/Berlin")
    today  = datetime.now(berlin).date()
    v = 0
    ips: set[str] = set()
    ip_pat = re.compile(r'^(\S+)')
    for log_file in _stats_log_files(2):
        for line in _stats_read_lines(log_file):
            if '"GET /kalender' not in line and '"GET / ' not in line:
                continue
            dt = _stats_parse_dt(line)
            if dt is None:
                continue
            if dt.replace(tzinfo=_tz.utc).astimezone(berlin).date() != today:
                continue
            m = ip_pat.match(line)
            ips.add(_stats_anon_ip(m.group(1)) if m else "unknown")
            v += 1
    return v, len(ips)


@kalender_bp.route("/api/admin/stats/chart", methods=["GET"])
def api_admin_stats_chart():
    token = request.headers.get("X-Upload-Token", "")
    if not UPLOAD_TOKEN or token != UPLOAD_TOKEN:
        return json.dumps({"error": "Nicht autorisiert"}), 401, {"Content-Type": "application/json"}

    from zoneinfo import ZoneInfo
    from shared.vk_db import get_page_stats
    berlin = ZoneInfo("Europe/Berlin")
    today  = datetime.now(berlin).date()

    try:
        d = int(request.args.get("d", "30"))
    except ValueError:
        d = 30
    d = min(max(d, 7), 3650)

    from_date = (today - timedelta(days=d - 1)).isoformat()
    to_date   = today.isoformat()

    db_rows = {r["datum"]: r for r in get_page_stats(from_date, to_date)}

    # Heute immer live aus Logs (Cron läuft erst um 00:05 für den Vortag)
    tv, tu = _count_today_live()
    db_rows[today.isoformat()] = {"datum": today.isoformat(), "views": tv, "unique_visitors": tu}

    result = []
    for i in range(d - 1, -1, -1):
        day = (today - timedelta(days=i)).isoformat()
        r   = db_rows.get(day)
        result.append({
            "datum":  day,
            "views":  r["views"]           if r else 0,
            "unique": r["unique_visitors"]  if r else 0,
        })

    return json.dumps({"tage": result}, ensure_ascii=False), 200, {
        "Content-Type": "application/json; charset=utf-8",
        "Cache-Control": "no-store",
    }


@kalender_bp.route("/api/admin/stats/hourly", methods=["GET"])
def api_admin_stats_hourly():
    token = request.headers.get("X-Upload-Token", "")
    if not UPLOAD_TOKEN or token != UPLOAD_TOKEN:
        return json.dumps({"error": "Nicht autorisiert"}), 401, {"Content-Type": "application/json"}

    from zoneinfo import ZoneInfo
    berlin = ZoneInfo("Europe/Berlin")
    today  = datetime.now(berlin).date()

    try:
        d = int(request.args.get("d", "30"))
    except ValueError:
        d = 30
    d = min(max(d, 1), 365)
    from_date = (today - timedelta(days=d - 1)).isoformat()

    # Stunden aus DB aggregieren
    hourly = [0] * 24
    try:
        with db_conn() as conn:
            rows = conn.execute(
                "SELECT stunde, SUM(views) AS total FROM page_stats_hourly "
                "WHERE datum >= ? GROUP BY stunde",
                (from_date,),
            ).fetchall()
            for r in rows:
                if 0 <= r["stunde"] <= 23:
                    hourly[r["stunde"]] = r["total"] or 0
    except Exception:
        pass

    # Heute live aus Log hinzuzählen
    try:
        import re as _re, gzip as _gzip
        from pathlib import Path as _Path
        from zoneinfo import ZoneInfo as _ZI
        _berlin = _ZI("Europe/Berlin")
        _log = _Path("/var/log/nginx/vereinskalender.access.log")
        _months = {"Jan":1,"Feb":2,"Mar":3,"Apr":4,"May":5,"Jun":6,
                   "Jul":7,"Aug":8,"Sep":9,"Oct":10,"Nov":11,"Dec":12}
        if _log.exists():
            for line in _log.read_text(errors="ignore").splitlines():
                if '"GET /kalender' not in line and '"GET / ' not in line:
                    continue
                m = _re.search(r'\[(\d{2})/(\w{3})/(\d{4}):(\d{2}):(\d{2}):(\d{2})', line)
                if not m:
                    continue
                dd, mo, yy, hh = m.group(1), m.group(2), m.group(3), m.group(4)
                try:
                    from datetime import timezone as _tz
                    dt = datetime(int(yy), _months[mo], int(dd), int(hh), tzinfo=_tz.utc)
                    if dt.astimezone(_berlin).date() == today:
                        hourly[dt.astimezone(_berlin).hour] += 1
                except Exception:
                    pass
    except Exception:
        pass

    return json.dumps({"stunden": hourly, "tage": d}, ensure_ascii=False), 200, {
        "Content-Type": "application/json; charset=utf-8",
        "Cache-Control": "no-store",
    }


@kalender_bp.route("/api/admin/stats", methods=["GET"])
def api_admin_stats():
    token = request.headers.get("X-Upload-Token", "")
    if not UPLOAD_TOKEN or token != UPLOAD_TOKEN:
        return json.dumps({"error": "Nicht autorisiert"}), 401, {"Content-Type": "application/json"}

    h_views, w_views, h_unique, w_unique = _count_page_views()

    vereine_gesamt = vereine_aktiv = termine_kd = 0
    try:
        raw   = json.loads(VEREINSTERMINE_FILE.read_text()) if VEREINSTERMINE_FILE.exists() else {}
        heute = datetime.now().strftime("%Y-%m-%d")
        for key, items in raw.items():
            if key.startswith("_") or not isinstance(items, list):
                continue
            vereine_gesamt += 1
            kuenftige = [t for t in items if not t.get("geloescht") and t.get("datum", "") >= heute]
            if kuenftige:
                vereine_aktiv += 1
            termine_kd += len(kuenftige)
    except Exception:
        pass

    letzter_import = "–"
    last_import_file = Path("/opt/rename-webhook/last_import.json")
    try:
        if last_import_file.exists():
            li = json.loads(last_import_file.read_text())
            dt = datetime.strptime(li["datum"], "%Y-%m-%d %H:%M")
            letzter_import = f"{dt.strftime('%d.%m.%Y, %H:%M')} ({li['termine']} Termine, {li['vereine']} Vereine)"
    except Exception:
        pass

    from zoneinfo import ZoneInfo
    jetzt = datetime.now(ZoneInfo("Europe/Berlin")).strftime("%d.%m.%Y, %H:%M")

    tg_subscribers = 0
    ical_7d = 0
    ical_30d = 0
    try:
        from datetime import date as _d, timedelta as _td
        _heute = _d.today()
        _d7  = (_heute - _td(days=7)).isoformat()
        _d30 = (_heute - _td(days=30)).isoformat()
        with db_conn() as conn:
            r = conn.execute("SELECT COUNT(DISTINCT chat_id) AS n FROM tg_subscriptions").fetchone()
            tg_subscribers = r["n"] if r else 0
            r = conn.execute("SELECT COUNT(*) AS n FROM ical_feed_requests WHERE date >= ?", (_d7,)).fetchone()
            ical_7d = r["n"] if r else 0
            r = conn.execute("SELECT COUNT(*) AS n FROM ical_feed_requests WHERE date >= ?", (_d30,)).fetchone()
            ical_30d = r["n"] if r else 0
    except Exception:
        pass

    return json.dumps({
        "aufrufe_heute":   h_views,
        "aufrufe_7d":      w_views,
        "unique_heute":    h_unique,
        "unique_7d":       w_unique,
        "vereine_gesamt":  vereine_gesamt,
        "vereine_aktiv":   vereine_aktiv,
        "termine_kd":      termine_kd,
        "letzter_import":  letzter_import,
        "timestamp":       jetzt,
        "tg_subscribers":  tg_subscribers,
        "ical_7d":         ical_7d,
        "ical_30d":        ical_30d,
    }, ensure_ascii=False), 200, {"Content-Type": "application/json; charset=utf-8", "Cache-Control": "no-store"}


@kalender_bp.route("/api/confirm-import", methods=["POST"])
def api_confirm_import():
    token = request.headers.get("X-Upload-Token", "")
    if not UPLOAD_TOKEN or token != UPLOAD_TOKEN:
        return json.dumps({"error": "Nicht autorisiert"}), 401, {"Content-Type": "application/json"}

    body               = request.get_json(silent=True) or {}
    import_id          = body.get("import_id", "")
    confirm_list       = [o.strip() for o in body.get("confirm", []) if o.strip()]
    reject_list        = [o.strip() for o in body.get("reject",  []) if o.strip()]
    verein_ortschaften = {k: v for k, v in (body.get("verein_ortschaften") or {}).items() if v and v.strip()}

    pending_path = Path(f"/tmp/vk_pending_{import_id}.json")
    if not pending_path.exists():
        return json.dumps({"error": "Import nicht gefunden oder abgelaufen"}), 404, {"Content-Type": "application/json"}

    try:
        pending  = json.loads(pending_path.read_text())
        alle     = pending["alle"]
        auto_plz = pending.get("auto_plz", "")
        form_plz = pending.get("form_plz", "")

        try:
            data = json.loads(VEREINSTERMINE_FILE.read_text()) if VEREINSTERMINE_FILE.exists() else {}
        except Exception:
            data = {}

        ort_cfg   = data.get("_ortschaften", {"whitelist": [], "blacklist": []})
        whitelist = set(ort_cfg.get("whitelist", []))
        blacklist = set(ort_cfg.get("blacklist", []))
        for o in confirm_list:
            whitelist.add(o); blacklist.discard(o)
        for o in reject_list:
            blacklist.add(o); whitelist.discard(o)
        data["_ortschaften"] = {"whitelist": sorted(whitelist), "blacklist": sorted(blacklist)}

        result_vereine, total = _do_save_import(alle, auto_plz, form_plz, data, verein_ortschaften)
        pending_path.unlink(missing_ok=True)
        log(f"✅  Confirm-Import: {total} Termine, +{len(confirm_list)} whitelist, +{len(reject_list)} blacklist")

        return (
            json.dumps({"success": True, "vereine": result_vereine, "total": total}, ensure_ascii=False),
            200,
            {"Content-Type": "application/json; charset=utf-8"},
        )
    except Exception as e:
        log(f"❌  /api/confirm-import: {e}")
        return json.dumps({"error": str(e)}), 500, {"Content-Type": "application/json"}


def _get_rubrik(key: str, name: str, meta_entry: dict) -> str:
    if "rubrik" in meta_entry:
        return meta_entry["rubrik"]
    if "pfarr" in name.lower():
        return "Pfarrei"
    return "Verein"


@kalender_bp.route("/api/vereine", methods=["GET"])
def api_vereine_get():
    token = request.headers.get("X-Upload-Token", "")
    if not UPLOAD_TOKEN or token != UPLOAD_TOKEN:
        return json.dumps({"error": "Nicht autorisiert"}), 401, {"Content-Type": "application/json"}
    try:
        raw = json.loads(VEREINSTERMINE_FILE.read_text()) if VEREINSTERMINE_FILE.exists() else {}
    except Exception:
        raw = {}
    labels = raw.get("_labels", {})
    meta   = raw.get("_meta", {})
    result = []
    for key, name in labels.items():
        m          = meta.get(key, {})
        parts      = name.strip().split()
        last_word  = parts[-1].split("/")[0] if parts else ""
        derived    = last_word if len(last_word) > 4 else ""
        result.append({
            "key":                key,
            "name":               name,
            "heimatort":          m.get("heimatort", derived),
            "heimatort_explizit": "heimatort" in m,
            "plz":                m.get("plz", ""),
            "gemeinde":           m.get("gemeinde", ""),
            "landkreis":          m.get("landkreis", ""),
            "rubrik":             _get_rubrik(key, name, m),
        })
    n_termine = {}
    for vkey, events in raw.items():
        if vkey.startswith("_") or not isinstance(events, list):
            continue
        n_termine[vkey] = sum(1 for t in events if not t.get("geloescht"))
    for r in result:
        r["nTermine"] = n_termine.get(r["key"], 0)
    result.sort(key=lambda x: x["name"].lower())
    return json.dumps(result, ensure_ascii=False), 200, {
        "Content-Type": "application/json; charset=utf-8", "Cache-Control": "no-cache"}


@kalender_bp.route("/api/vereine", methods=["POST"])
def api_vereine_post():
    token = request.headers.get("X-Upload-Token", "")
    if not UPLOAD_TOKEN or token != UPLOAD_TOKEN:
        return json.dumps({"error": "Nicht autorisiert"}), 401, {"Content-Type": "application/json"}
    body = request.get_json(silent=True) or {}
    key  = body.get("key", "").strip()
    if not key:
        return json.dumps({"error": "key fehlt"}), 400, {"Content-Type": "application/json"}
    try:
        raw = json.loads(VEREINSTERMINE_FILE.read_text()) if VEREINSTERMINE_FILE.exists() else {}
    except Exception:
        raw = {}
    labels = raw.get("_labels", {})
    if key not in labels:
        return json.dumps({"error": "Verein nicht gefunden"}), 404, {"Content-Type": "application/json"}
    if body.get("name", "").strip():
        labels[key]   = body["name"].strip()
        raw["_labels"] = labels
    raw.setdefault("_meta", {}).setdefault(key, {})
    m = raw["_meta"][key]
    if "rubrik" in body:
        rubrik = body["rubrik"].strip()
        if rubrik:
            m["rubrik"] = rubrik
        else:
            m.pop("rubrik", None)
    if "heimatort" in body:
        ort = body["heimatort"].strip()
        if ort:
            m["heimatort"] = ort
        else:
            m.pop("heimatort", None)
    plz = body.get("plz", "").strip()
    if plz and re.match(r"^\d{5}$", plz):
        saved_heimatort = m.get("heimatort")
        new_meta        = lookup_plz(plz)
        m.update(new_meta)
        if saved_heimatort:
            m["heimatort"] = saved_heimatort
    from shared.kalender_store import KalenderStore
    KalenderStore.update(lambda d: d.clear() or d.update(raw))
    log(f"✏️  Verein {key} ({labels.get(key)}) aktualisiert")
    m2    = raw["_meta"].get(key, {})
    parts = labels.get(key, "").strip().split()
    lw    = parts[-1].split("/")[0] if parts else ""
    return json.dumps({
        "key": key, "name": labels.get(key, ""),
        "heimatort": m2.get("heimatort", lw if len(lw) > 4 else ""),
        "heimatort_explizit": "heimatort" in m2,
        "plz": m2.get("plz", ""), "gemeinde": m2.get("gemeinde", ""),
        "landkreis": m2.get("landkreis", ""),
        "rubrik": _get_rubrik(key, labels.get(key, ""), m2),
    }, ensure_ascii=False), 200, {"Content-Type": "application/json; charset=utf-8"}


@kalender_bp.route("/api/vereine/plz/<plz>", methods=["GET"])
def api_vereine_plz(plz):
    token = request.headers.get("X-Upload-Token", "")
    if not UPLOAD_TOKEN or token != UPLOAD_TOKEN:
        return json.dumps({"error": "Nicht autorisiert"}), 401, {"Content-Type": "application/json"}
    if not re.match(r"^\d{5}$", plz):
        return json.dumps({"error": "Ungültige PLZ"}), 400, {"Content-Type": "application/json"}
    return json.dumps(lookup_plz(plz), ensure_ascii=False), 200, {"Content-Type": "application/json; charset=utf-8"}




@kalender_bp.route("/api/vereine/<key>", methods=["DELETE"])
def api_vereine_delete(key):
    token = request.headers.get("X-Upload-Token", "")
    if not UPLOAD_TOKEN or token != UPLOAD_TOKEN:
        return json.dumps({"error": "Nicht autorisiert"}), 401, {"Content-Type": "application/json"}
    try:
        raw = json.loads(VEREINSTERMINE_FILE.read_text()) if VEREINSTERMINE_FILE.exists() else {}
    except Exception:
        raw = {}
    if key not in raw.get("_labels", {}):
        return json.dumps({"error": "Verein nicht gefunden"}), 404, {"Content-Type": "application/json"}
    name = raw["_labels"].pop(key, key)
    raw.get("_meta", {}).pop(key, None)
    vorher = len(raw.pop(key, []))
    geloescht = vorher
    from shared.kalender_store import KalenderStore
    KalenderStore.update(lambda d: d.clear() or d.update(raw))
    log("Verein geloescht: " + key + " (" + name + "), " + str(geloescht) + " Termine entfernt")
    return json.dumps({"ok": True, "geloescht": geloescht}, ensure_ascii=False), 200, {"Content-Type": "application/json"}

@kalender_bp.route("/api/ortschaften", methods=["GET"])
def api_ortschaften_get():
    try:
        raw = json.loads(VEREINSTERMINE_FILE.read_text()) if VEREINSTERMINE_FILE.exists() else {}
    except Exception:
        raw = {}
    ort = raw.get("_ortschaften", {"whitelist": [], "blacklist": []})
    return json.dumps(ort, ensure_ascii=False), 200, {
        "Content-Type": "application/json; charset=utf-8", "Cache-Control": "no-cache"}


@kalender_bp.route("/api/ortschaften", methods=["POST"])
def api_ortschaften_post():
    token = request.headers.get("X-Upload-Token", "")
    if not UPLOAD_TOKEN or token != UPLOAD_TOKEN:
        return json.dumps({"error": "Nicht autorisiert"}), 401, {"Content-Type": "application/json"}

    body   = request.get_json(silent=True) or {}
    name   = (body.get("name") or "").strip()
    action = body.get("action", "")

    if not name or action not in ("whitelist", "blacklist", "delete"):
        return json.dumps({"error": "Ungültige Parameter"}), 400, {"Content-Type": "application/json"}

    try:
        data = json.loads(VEREINSTERMINE_FILE.read_text()) if VEREINSTERMINE_FILE.exists() else {}
    except Exception:
        data = {}

    ort_cfg   = data.get("_ortschaften", {"whitelist": [], "blacklist": []})
    whitelist = set(ort_cfg.get("whitelist", []))
    blacklist = set(ort_cfg.get("blacklist", []))
    if action == "whitelist":
        whitelist.add(name); blacklist.discard(name)
    elif action == "blacklist":
        blacklist.add(name); whitelist.discard(name)
    elif action == "delete":
        whitelist.discard(name); blacklist.discard(name)

    data["_ortschaften"] = {"whitelist": sorted(whitelist), "blacklist": sorted(blacklist)}
    from shared.kalender_store import KalenderStore
    KalenderStore.update(lambda d: d.update({"_ortschaften": data["_ortschaften"]}))
    log(f"📍  Ortschaft {action}: {name}")
    return json.dumps({"success": True}, ensure_ascii=False), 200, {"Content-Type": "application/json"}


@kalender_bp.route("/api/termine")
def api_termine():
    try:
        raw = json.loads(VEREINSTERMINE_FILE.read_text())
    except Exception:
        raw = {}
    labels = raw.get("_labels", {})
    labels.setdefault("ff", "FF Hölskofen")
    labels.setdefault("kp", "Königstreue Patrioten Hölskofen")
    termine = []
    for key, events in raw.items():
        if key.startswith("_") or not isinstance(events, list):
            continue
        for t in events:
            termine.append({**t, "verein": key})

    _hat_pfarrgemeinde = any(k.startswith("pfarrgemeinde") for k in raw if not k.startswith("_"))
    gf = GOTTESDIENSTE_FILE
    if gf.exists() and not _hat_pfarrgemeinde:
        try:
            gd = json.loads(gf.read_text())
            for bereich, items in gd.items():
                if not isinstance(items, list):
                    continue
                vkey = _PG_KEYS.get(bereich)
                if not vkey:
                    continue
                labels[vkey] = _PG_LABELS[vkey]
                for t in items:
                    termine.append({
                        "datum":       t.get("datum", ""),
                        "uhrzeit":     t.get("uhrzeit", ""),
                        "ort":         t.get("ort", ""),
                        "bezeichnung": t.get("art", ""),
                        "verein":      vkey,
                    })
        except Exception as e:
            log(f"⚠️  Gottesdienste in API: {e}")

    json_meta   = raw.get("_meta", {})
    ortschaften = raw.get("_ortschaften", {"whitelist": [], "blacklist": []})

    # DB-Meta laden (Vereinsadmin-Selbstverwaltung, Prio 2); JSON überschreibt (Superadmin, Prio 1)
    try:
        with db_conn() as conn:
            db_rows = conn.execute(
                """SELECT verein_key, rubrik, heimatort, plz, gemeinde, landkreis
                   FROM vereine_accounts WHERE verein_key IS NOT NULL"""
            ).fetchall()
    except Exception:
        db_rows = []
    db_meta = {}
    for row in db_rows:
        entry = {}
        for col in ("rubrik", "heimatort", "plz", "gemeinde", "landkreis"):
            if row[col]:
                entry[col] = row[col]
        if entry:
            db_meta[row["verein_key"]] = entry

    merged_meta = {}
    for key in set(list(json_meta.keys()) + list(db_meta.keys())):
        m = {**db_meta.get(key, {}), **json_meta.get(key, {})}
        if m:
            merged_meta[key] = m

    rubriken    = {k: _get_rubrik(k, v, merged_meta.get(k, {})) for k, v in labels.items()}
    return (
        json.dumps({"labels": labels, "termine": termine, "meta": merged_meta,
                    "ortschaften": ortschaften, "rubriken": rubriken}, ensure_ascii=False),
        200,
        {"Content-Type": "application/json; charset=utf-8", "Cache-Control": "no-cache, must-revalidate"},
    )


@kalender_bp.route("/api/termine", methods=["PATCH"])
def api_termine_patch():
    token = request.headers.get("X-Upload-Token", "")
    if not UPLOAD_TOKEN or token != UPLOAD_TOKEN:
        return json.dumps({"error": "Nicht autorisiert"}), 401, {"Content-Type": "application/json"}
    body = request.get_json(force=True, silent=True) or {}
    verein_key = body.get("verein_key", "")
    old_datum = body.get("datum", "")
    old_bezeichnung = body.get("bezeichnung", "")
    new_verein_key = body.get("new_verein_key", "").strip()
    changes = {k: v for k, v in body.get("changes", {}).items()
               if k in {"datum", "uhrzeit", "ort", "ortschaft", "bezeichnung"}}
    if not verein_key or not old_datum or not old_bezeichnung:
        return json.dumps({"error": "verein_key, datum und bezeichnung erforderlich"}), 400, {"Content-Type": "application/json"}
    found = [False]
    _sort = lambda lst: sorted(lst, key=lambda x: (x.get("datum", ""), x.get("uhrzeit", "")))
    def mutator(data):
        liste = data.get(verein_key, [])
        for i, t in enumerate(liste):
            if t.get("datum") == old_datum and t.get("bezeichnung") == old_bezeichnung:
                t.update(changes)
                found[0] = True
                if new_verein_key and new_verein_key != verein_key:
                    moved = liste.pop(i)
                    data[verein_key] = _sort(liste)
                    data.setdefault(new_verein_key, []).append(moved)
                    data[new_verein_key] = _sort(data[new_verein_key])
                elif "datum" in changes:
                    data[verein_key] = _sort(liste)
                break
    from shared.kalender_store import KalenderStore
    KalenderStore.update(mutator)
    if not found[0]:
        return json.dumps({"error": "Termin nicht gefunden"}), 404, {"Content-Type": "application/json"}
    log(f"Termin bearbeitet: {verein_key} / {old_datum} / {old_bezeichnung}" + (f" → {new_verein_key}" if new_verein_key else ""))
    return json.dumps({"ok": True}, ensure_ascii=False), 200, {"Content-Type": "application/json; charset=utf-8"}


@kalender_bp.route("/api/termine", methods=["DELETE"])
def api_termine_delete():
    token = request.headers.get("X-Upload-Token", "")
    if not UPLOAD_TOKEN or token != UPLOAD_TOKEN:
        return json.dumps({"error": "Nicht autorisiert"}), 401, {"Content-Type": "application/json"}
    body = request.get_json(force=True, silent=True) or {}
    verein_key = body.get("verein_key", "")
    old_datum = body.get("datum", "")
    old_bezeichnung = body.get("bezeichnung", "")
    if not verein_key or not old_datum or not old_bezeichnung:
        return json.dumps({"error": "verein_key, datum und bezeichnung erforderlich"}), 400, {"Content-Type": "application/json"}
    found = [False]
    def mutator(data):
        liste = data.get(verein_key, [])
        neue_liste = [t for t in liste if not (t.get("datum") == old_datum and t.get("bezeichnung") == old_bezeichnung)]
        found[0] = len(neue_liste) < len(liste)
        data[verein_key] = neue_liste
    from shared.kalender_store import KalenderStore
    KalenderStore.update(mutator)
    if not found[0]:
        return json.dumps({"error": "Termin nicht gefunden"}), 404, {"Content-Type": "application/json"}
    log(f"Termin geloescht: {verein_key} / {old_datum} / {old_bezeichnung}")
    return json.dumps({"ok": True}, ensure_ascii=False), 200, {"Content-Type": "application/json; charset=utf-8"}


@kalender_bp.route("/api/ical")
def api_ical():
    datum   = request.args.get("d", "").strip()
    titel   = request.args.get("t", "").strip()
    label   = request.args.get("v", "").strip()
    uhrzeit = request.args.get("u", "").strip()
    ort     = request.args.get("o", "").strip()

    if not datum or not titel:
        return "Pflichtfelder fehlen", 400
    try:
        y, mo, d = [int(x) for x in datum.split("-")]
    except Exception:
        return "Ungültiges Datum", 400

    def _p(n): return str(n).zfill(2)

    if uhrzeit:
        try:
            hh, mm = [int(x) for x in uhrzeit.split(":")]
        except Exception:
            return "Ungültige Uhrzeit", 400
        eh     = hh + 1 if hh < 23 else 23
        em     = mm if hh < 23 else 59
        dtstart = f"DTSTART:{y}{_p(mo)}{_p(d)}T{_p(hh)}{_p(mm)}00"
        dtend   = f"DTEND:{y}{_p(mo)}{_p(d)}T{_p(eh)}{_p(em)}00"
    else:
        nd      = date(y, mo, d) + timedelta(days=1)
        dtstart = f"DTSTART;VALUE=DATE:{y}{_p(mo)}{_p(d)}"
        dtend   = f"DTEND;VALUE=DATE:{nd.year}{_p(nd.month)}{_p(nd.day)}"

    uid  = f"{datum}-{re.sub(r'[^a-z0-9]', '', titel.lower()[:20])}-{int(time.time())}@vereinskalender"
    desc = label + (f"\\n{ort}" if ort else "")

    lines = [
        "BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//Vereinskalender//DE",
        "BEGIN:VEVENT",
        f"UID:{uid}",
        dtstart, dtend,
        f"SUMMARY:{titel.replace(',', chr(92) + ',')}",
        f"DESCRIPTION:{desc.replace(',', chr(92) + ',')}",
    ]
    if ort:
        lines.append(f"LOCATION:{ort.replace(',', chr(92) + ',')}")
    lines += ["TRANSP:TRANSPARENT", "STATUS:CONFIRMED", "END:VEVENT", "END:VCALENDAR"]

    safe = re.sub(r"[^\wäöüÄÖÜß]", "-", titel).strip("-")[:40]
    return Response(
        "\r\n".join(lines),
        mimetype="text/calendar; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{datum}-{safe}.ics"'},
    )


def _track_ical_request():
    import hashlib
    ip = (request.headers.get("X-Forwarded-For") or request.remote_addr or "").split(",")[0].strip()
    try:
        addr = ipaddress.ip_address(ip)
        if addr.version == 6:
            prefix = str(ipaddress.ip_network(f"{ip}/48", strict=False).network_address)
        else:
            prefix = ".".join(ip.split(".")[:3])
    except Exception:
        prefix = ip[:20]
    ip_hash = hashlib.sha256(prefix.encode()).hexdigest()[:20]
    today = datetime.now().date().isoformat()
    try:
        with db_conn() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO ical_feed_requests (date, ip_hash) VALUES (?, ?)",
                (today, ip_hash),
            )
    except Exception:
        pass


@kalender_bp.route("/api/ical/feed")
def api_ical_feed():
    """Abonnierbarer iCal-Feed aller bevorstehenden Termine (webcal://)."""
    _track_ical_request()
    filter_vereine = {v.strip().lower() for v in request.args.get("v", "").split(",") if v.strip()}
    filter_ort     = request.args.get("ort", "").strip().lower()

    try:
        raw = json.loads(VEREINSTERMINE_FILE.read_text())
    except Exception:
        raw = {}

    labels = raw.get("_labels", {})
    labels.setdefault("ff", "FF Hölskofen")
    labels.setdefault("kp", "Königstreue Patrioten Hölskofen")

    heute = datetime.now().date()
    alle  = []

    for key, events in raw.items():
        if key.startswith("_") or not isinstance(events, list):
            continue
        if filter_vereine and key not in filter_vereine:
            continue
        for t in events:
            alle.append({**t, "_vkey": key})

    _hat_pfarrgemeinde = any(k.startswith("pfarrgemeinde") for k in raw if not k.startswith("_"))
    gf = GOTTESDIENSTE_FILE
    if gf.exists() and not _hat_pfarrgemeinde and (not filter_vereine or filter_vereine & set(_PG_KEYS.values())):
        try:
            gd = json.loads(gf.read_text())
            for bereich, items in gd.items():
                if not isinstance(items, list):
                    continue
                vkey = _PG_KEYS.get(bereich)
                if not vkey:
                    continue
                if filter_vereine and vkey not in filter_vereine:
                    continue
                for t in items:
                    alle.append({
                        "datum":       t.get("datum", ""),
                        "uhrzeit":     t.get("uhrzeit", ""),
                        "ort":         t.get("ort", ""),
                        "bezeichnung": t.get("art", ""),
                        "_vkey":       vkey,
                    })
        except Exception:
            pass

    kuenftige = sorted(
        [t for t in alle if t.get("datum", "") >= heute.strftime("%Y-%m-%d")
         and (not filter_ort or filter_ort in t.get("ort", "").lower()
              or filter_ort in t.get("ortschaft", "").lower())],
        key=lambda t: (t["datum"], t.get("uhrzeit", ""))
    )

    def _p(n): return str(n).zfill(2)

    now_stamp    = datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
    vevent_lines = []

    for t in kuenftige:
        try:
            y, mo, d = [int(x) for x in t["datum"].split("-")]
        except Exception:
            continue

        bezeichnung = t.get("bezeichnung") or t.get("art") or "Termin"
        ort         = t.get("ort", "")
        vkey        = t.get("_vkey", "")
        vereinname  = labels.get(vkey, vkey.upper())
        uhrzeit     = t.get("uhrzeit", "")

        if uhrzeit:
            try:
                hh, mm = [int(x) for x in uhrzeit.split(":")]
            except Exception:
                hh, mm = 0, 0
            eh     = hh + 1 if hh < 23 else 23
            dtstart = f"DTSTART:{y}{_p(mo)}{_p(d)}T{_p(hh)}{_p(mm)}00"
            dtend   = f"DTEND:{y}{_p(mo)}{_p(d)}T{_p(eh)}{_p(mm)}00"
        else:
            nd      = date(y, mo, d) + timedelta(days=1)
            dtstart = f"DTSTART;VALUE=DATE:{y}{_p(mo)}{_p(d)}"
            dtend   = f"DTEND;VALUE=DATE:{nd.year}{_p(nd.month)}{_p(nd.day)}"

        uid_raw = f"{t['datum']}-{re.sub(r'[^a-z0-9]', '', bezeichnung.lower()[:20])}-{vkey}@vereinskalender"
        desc    = vereinname + (f"\\n{ort}" if ort else "")

        vevent_lines += [
            "BEGIN:VEVENT",
            f"UID:{uid_raw}",
            f"DTSTAMP:{now_stamp}",
            dtstart, dtend,
            f"SUMMARY:{bezeichnung}",
            f"DESCRIPTION:{desc}",
        ]
        if ort:
            vevent_lines.append(f"LOCATION:{ort}")
        vevent_lines += ["TRANSP:TRANSPARENT", "STATUS:CONFIRMED", "END:VEVENT"]

    cal_name = "Vereinskalender"
    if filter_vereine:
        namen    = [labels.get(k, k.upper()) for k in sorted(filter_vereine)]
        cal_name = ", ".join(namen) if len(namen) <= 3 else f"Vereinskalender ({len(namen)} Vereine)"

    header = [
        "BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//Vereinskalender//DE",
        "CALSCALE:GREGORIAN", "METHOD:PUBLISH",
        f"X-WR-CALNAME:{cal_name}",
        "X-WR-TIMEZONE:Europe/Berlin",
        "REFRESH-INTERVAL;VALUE=DURATION:PT6H",
        "X-PUBLISHED-TTL:PT6H",
    ]
    footer   = ["END:VCALENDAR"]
    ics_body = "\r\n".join(header + vevent_lines + footer)
    return Response(
        ics_body,
        mimetype="text/calendar; charset=utf-8",
        headers={"Content-Disposition": "inline; filename=\"vereinskalender.ics\""},
    )
