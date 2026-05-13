#!/opt/rename-webhook/bin/python3
"""
traffic_info.py
Cron Di+Do 06:00: Verkehrsinfo Hölskofen → CrossFit München per Telegram.
"""

import json
import sys
import urllib.request
import urllib.parse
from datetime import datetime

sys.path.insert(0, "/opt/rename-webhook")
from shared.secrets import load_secrets
from shared.telegram import send_telegram

ORIGIN      = "Hölskofen, Pfeffenhausen, Bayern, Deutschland"
DESTINATION = "Frankfurter Ring 255, 80807 München"
LOG         = "/var/log/pka-traffic.log"

WOCHENTAGE = ["Mo", "Di", "Mi", "Do", "Fr", "Sa", "So"]


def get_route(api_key: str) -> tuple[int, int, int]:
    """Gibt (normal_sek, traffic_sek, distanz_m) zurück."""
    params = urllib.parse.urlencode({
        "origin":         ORIGIN,
        "destination":    DESTINATION,
        "departure_time": "now",
        "traffic_model":  "best_guess",
        "key":            api_key,
    })
    url = f"https://maps.googleapis.com/maps/api/directions/json?{params}"
    with urllib.request.urlopen(url, timeout=15) as r:
        data = json.loads(r.read())

    if data["status"] != "OK":
        raise RuntimeError(f"Directions API: {data['status']} – {data.get('error_message', '')}")

    leg     = data["routes"][0]["legs"][0]
    normal  = leg["duration"]["value"]
    traffic = leg["duration_in_traffic"]["value"]
    dist    = leg["distance"]["value"]
    return normal, traffic, dist


def fmt_dauer(sek: int) -> str:
    h, m = divmod(sek // 60, 60)
    return f"{h} Std {m} Min" if h else f"{m} Min"


def main():
    now     = datetime.now()
    tag     = WOCHENTAGE[now.weekday()]
    datum   = now.strftime(f"{tag}, %d.%m.%Y")

    secrets = load_secrets()
    api_key = secrets["GOOGLE_MAPS_API_KEY"]
    token   = secrets["TOKEN"]
    chat_id = secrets["CHAT_ID"]

    normal, traffic, dist = get_route(api_key)
    diff_min = max(0, (traffic - normal) // 60)

    if diff_min < 10:
        ampel   = "🟢"
        hinweis = "Straße ist frei – normal losfahren."
    elif diff_min < 20:
        ampel   = "🟡"
        hinweis = "Etwas Verkehr – etwas früher losfahren."
    else:
        ampel   = "🔴"
        hinweis = f"Stau! {diff_min} Min Verzögerung – deutlich früher losfahren."

    zeilen = [
        f"🚗 Verkehr: Hölskofen → CrossFit München",
        f"{datum} – 06:00 Uhr",
        "",
        f"📍 Strecke: {dist / 1000:.0f} km",
        f"⏱ Normale Fahrt:  {fmt_dauer(normal)}",
        f"{ampel} Mit Verkehr:   {fmt_dauer(traffic)}" + (f"  (+{diff_min} Min)" if diff_min else ""),
        "",
        f"→ {hinweis}",
    ]
    nachricht = "\n".join(zeilen)

    send_telegram(token, chat_id, nachricht)

    log_line = f"{now.isoformat()} – {fmt_dauer(traffic)} (+{diff_min} Min) | {hinweis}"
    print(log_line)
    with open(LOG, "a") as f:
        f.write(log_line + "\n")


if __name__ == "__main__":
    main()
