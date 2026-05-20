# Vereinskalender – Claude-Kontext

## Kerninfos

- **GitHub (Source of Truth):** `https://github.com/sEppofaz/Vereinskalender`
- **Lokale Arbeitskopie:** `~/Dropbox/Apps/Claude/Vereinskalender/src/` – hier bearbeiten, dann `git push`
- **Auf Server:** `/opt/rename-webhook/` – zieht per `git pull` von GitHub
- **Credentials:** ausschließlich in `/etc/pka/secrets.env` (via EnvironmentFile im Service)
- **Deployment-SOP:** `PKA/SOPs/Vereinskalender-Deployment.md`

### Deployment-Flow (Mac → GitHub → Hetzner)
```bash
cd ~/Library/CloudStorage/Dropbox/Apps/Claude/Vereinskalender/src
git add . && git commit -m "Beschreibung" && git push
ssh root@89.167.104.145 "git -C /opt/rename-webhook pull && systemctl restart rename-webhook"
```

### Code vom Server ziehen (Ausnahme!)
```bash
ssh root@89.167.104.145 "git -C /opt/rename-webhook add . && git -C /opt/rename-webhook commit -m 'Hotfix' && git -C /opt/rename-webhook push"
# Dann lokal: git pull
```

---

## Kalender-Input via Dropbox

Dateien in `/Dokumente/Vereinskalender/input/` (Dropbox) werden automatisch verarbeitet:

1. Dropbox-Webhook triggert → webhook.py lädt Datei herunter
2. Claude Vision extrahiert Termine
3. Telegram-Nachricht mit Vorschau + Inline-Buttons **✅ Importieren / ❌ Verwerfen**
4. Datei wird sofort nach Extraktion nach `/Dokumente/Vereinskalender/verarbeitet/` verschoben
5. Bei ✅: Termine landen in `vereinstermine.json`; Telegram-Bestätigung mit Statistik
6. Bei ❌: Vorschau wird verworfen; Datei bleibt in `verarbeitet/`

**Pending-Store:** In-Memory (`_kalender_pending`, key = 8-stellige UUID). Bei Server-Neustart gehen offene Bestätigungen verloren → Datei erneut in `input/` legen.
**Cursor:** `/opt/rename-webhook/kalender_input_cursor.txt`
**Erlaubte Dateitypen:** PDF, JPG, PNG, HEIC, TIFF, WEBP, BMP, TXT, RTF

---

## heimat-info.de Import (heimat_import.py)

**Script:** `/opt/rename-webhook/heimat_import.py`
**Gemeinden-Konfiguration:** `/opt/rename-webhook/heimat_gemeinden.json`
**Log:** `/var/log/pka-heimat.log`

### Funktionsweise

1. Export-API: `https://heimatinfo-api-platform.azurewebsites.net/export/events?pageIndex=0&pageSize=50&c=<UUID>` (max. pageSize=50, CORS-Guard: Header `Origin: https://www.heimat-info.de` erforderlich)
2. UUID (`c=`) ist pro Gemeinde eindeutig – einmalig via Playwright ermittelt
3. `_fetch_all_events(c_id)` holt alle Events paginiert; `_parse_api_events()` parst JSON (UTC → Europe/Berlin via zoneinfo; T00:00:00Z = ganztägig)
4. **Duplikatprüfung:** `_existing_events()` → `set[(datum, uhrzeit, bezeichnung.lower())]` cross-key. `_is_duplicate()` prüft exakten Match + Substring-Match (nur wenn datum+uhrzeit übereinstimmen und Bezeichnung ≥ 6 Zeichen)
5. Bei ✅: `do_import(uid)` prüft `_neu`-Flag, dann `_is_duplicate()`; schreibt in `vereinstermine.json`
6. Bei ❌: Pending-Datei wird gelöscht

### Gemeinde hinzufügen
```bash
/heimat-add https://www.gemeinde-xyz.de/veranstaltungen/
# Oder direkt:
ssh root@89.167.104.145 "/opt/rename-webhook/bin/python3 /opt/rename-webhook/heimat_import.py --add https://..."
```

### Felder in heimat_gemeinden.json
```json
[{"name": "Bayerbach", "label": "Veranstaltungen Bayerbach", "verein_key": "bayerbach",
  "c_id": "77bc043e-...", "url": "https://www.gemeinde-bayerbach.de/veranstaltungen/"}]
```

### Pitfalls
- **Pending-Dir:** `/opt/rename-webhook/imports/heimat_pending_*.json` – persistiert Server-Neustart
- **Export-API pageSize-Limit:** Max. `pageSize=50`. Paginierung via `pageIndex=0,1,2…`
- **Export-API CORS-Guard:** Ohne `Origin: https://www.heimat-info.de` kommt HTTP 400
- **Ganztägige Termine:** `startDate` endet auf `T00:00:00Z` → kein Uhrzeitfeld
- **Log-Ownership:** `/var/log/pka-heimat.log` kann als `root` erstellt werden → `chown webhook:webhook`
- **`_meta.heimatort`:** Label-Format muss `"Veranstaltungen <Gemeindename>"` sein (letztes Wort = Heimatort-Fallback)
- **`veranstalter`-Feld:** Nur bei heimat-info-Importen. Badge-Fallback: `t.veranstalter || labels[t.verein] || t.verein`
- **Duplikat-Logik:** Substring-Check nur wenn datum + uhrzeit identisch
- **`_neu`-Flag in `do_import()`:** Erste Bedingung (vor `_is_duplicate()`). `_neu=False` schließt Event aus
- **`quelle`/`quelle_url`:** Werden aus Pending-Datei übernommen. `quelle = "heimat-info.de"`
- **Geo-Schutz in `do_import()`:** Geo-Felder werden nur gesetzt wenn `key not in data["_meta"]`
- **Borlabs Cookie:** `discover_c_id()` versucht zuerst Base64-Decode, fällt auf Playwright-Intercept zurück

---

## Cron-Jobs (Vereinskalender-relevant)

Alle Jobs als `root`-Crontab. Timezone: `Europe/Berlin`. Logs: `/var/log/pka-*.log`.

| Zeit | Script | Beschreibung |
|------|--------|--------------|
| täglich 06:30 | `logbuch_summary.py` | Logbuch-Eintrag per Telegram (nicht im Repo) |
| täglich 18:00 | `event_reminder.py` | Erinnerung morgige Gottesdienste + Vereinstermine |
| täglich 18:00 | `kalender_erinnerung.py` | Telegram-Erinnerungen für Bot-Abonnenten |
| täglich 00:10, 06:00, 13:00, 20:00 | `kalender_report.py` | Vereinskalender-Bericht |
| täglich 00:05 | `stats_collector.py` | Besucherstatistik → `page_stats`-Tabelle |
| quartalsweise 07:00 (1. Jan/Apr/Jul/Okt) | `heimat_import.py` | heimat-info.de alle Gemeinden fetchen |
| alle 15 Min | `pka_todos_reminder.py` | PKA Todos Fälligkeits-Erinnerungen |

---

## nginx-Konfiguration

- **Config:** `/etc/nginx/sites-available/vereinskalender` → Domain `vereinskalender.online`
- `location = /` → `proxy_pass http://127.0.0.1:5000/kalender` + `Cache-Control: no-store`
- `location = /admin` → `proxy_pass http://127.0.0.1:5000` + `Cache-Control: no-store`
- `location = /sw.js` → `Cache-Control: no-cache, no-store` + `Service-Worker-Allowed: /`
- `location = /api/termine` → Rate-Limit 30 req/min, Burst 5 (Scraping-Schutz)
- `location /api/` → Rate-Limit 10 req/s, Burst 30
- `location /verein` → proxy_pass Flask (Auth-Seiten, Dashboard)
- **Rate-Limit-Conf:** `/etc/nginx/conf.d/rate-limit.conf` (api_zone, api_termine_zone, auth_zone)
- **Security-Header:** HSTS, X-Frame-Options, X-Content-Type-Options, Referrer-Policy – in Locations mit eigenem `add_header` explizit wiederholen (nginx-Vererbungsregel)
- Nach Änderungen: `nginx -t && systemctl reload nginx`

---

## Öffentliche Endpunkte `vereinskalender.online`

| Pfad | Beschreibung |
|------|--------------|
| `/` | Vereinskalender-PWA |
| `/api/termine` | GET/PATCH/DELETE – Termine (PATCH/DELETE: Auth X-Upload-Token) |
| `/api/ical` | GET – iCal-Export einzelner Termin |
| `/api/ical/feed` | GET – Abonnierbarer Feed (`webcal://`), optional `?v=key1,key2` oder `?ort=Ortschaft` |
| `/api/check-token` | POST – Admin-Token prüfen |
| `/api/confirm-import` | POST – Upload-Import bestätigen |
| `/api/admin/importe` | GET – Pending-Liste |
| `/api/admin/importe/<uid>` | GET/confirm/reject – Import-Detail |
| `/api/admin/importe/trigger` | POST – Import-Trigger (SSRF-Schutz: nur HTTPS, keine privaten IPs) |
| `/api/vereine` | GET/POST – Vereine + Meta |
| `/api/vereine/<key>` | DELETE – Verein löschen |
| `/api/admin/stats` | GET – Statistiken (Auth) |
| `/api/admin/stats/chart` | GET – Tages-Zeitreihe `?d=7|30|365` |
| `/api/admin/users` | GET – Alle Accounts (Auth) |
| `/api/admin/verein/<id>` | PATCH/DELETE – Verein-Account |
| `/upload` | Superadmin-Upload (PDF/JPG/PNG/HEIC/Excel) |
| `/#admin` | Admin-PWA (Tabs: Import/Importe/Vereine/Accounts/Termine/Stats) |
| `/verein/register` | Selbstregistrierung |
| `/verein/login` | Vereins-Login (bcrypt, Brute-Force-Schutz) |
| `/verein/dashboard` | Termin-Übersicht (nach Login) |
| `/verein/upload` | Vereinsadmin-Upload (Rate-Limit 3/Tag) |
| `/sw.js` | Service Worker (Network-first App-Shell, /api/* nie cachen) |
| `/manifest.json` / `/manifest-admin.json` | PWA-Manifeste |

---

## Datenstruktur `vereinstermine.json`

- **`_labels`**: `{vereinKey: "Anzeigename"}` – letztes Wort > 4 Zeichen = Heimatort-Fallback
- **`_meta[key]`**: `{plz, gemeinde, landkreis, heimatort?, selbstverwaltung?}`
- **`_ortschaften`**: `{gemeinde_map: {...}}` – Mapping Ortschaft→Gemeinde
- **`ortschaft`**: Pro Termin – Veranstaltungsort
- **`quelle`** / **`quelle_url`**: Pro Termin – Herkunft (heimat-info oder Vereinsadmin)

**Ortschaft-Hierarchie:** `Landkreis → Gemeinde → Ortschaft (Vereinsheimat) → Verein → Termin`
- Ortschaft-Chips = Heimatort des Vereins, NICHT Veranstaltungsort
- `_vereinsForOrt(o)` → Set aller Verein-Keys für Ortschaft
- `_gemeindeForOrt(o)` → Gemeinde-String für Ortschaft

---

## Filterlogik kalender.html – wichtige Pitfalls

- **Rubrik-Filter:** Kein „Alle"-Toggle. Kein aktiver Chip = alle Rubriken sichtbar
- **Favoriten-Priorität:** Aktiver Favorit-Chip überschreibt Verein-Dropdown + Rubrik-Filter
- **Selbstverwaltungs-Schutz:** `_meta[key].selbstverwaltung = true` → Admin-Dialog vor Edit/Delete; heimat-Import überspringt diese Vereine
- **Landkreis-Fallback:** Vereine ohne `meta[key].landkreis` → `"Landkreis Landshut"`
- **Filter-Reihenfolge:** aktVereine → Rubrik → Landkreis → Zeitraum → Suche → Ortschaft → Favoriten
- **Offline:** `try/catch` um `/api/termine`-Fetch → Meldung „🔇 Keine Internetverbindung"

---

## Upload-Workflow (zweistufig)

1. PDF/Foto → Claude Vision extrahiert Termine (verein, datum, ort, ortschaft, bezeichnung)
2. Neue Vereine ohne Heimatort → Admin gibt Heimatort ein
3. Admin bestätigt → `/api/confirm-import` speichert Termine + `heimatort` in `_meta`

**Vereinsadmin-Upload:** `/verein/upload` – PDF/Foto oder Excel (5 Spalten, verein aus Session); Rate-Limit 3/Tag; Quota vor Claude-Call erhöht.

---

## Besucherstatistik

- **Script:** `stats_collector.py` (täglich 00:05 via Cron)
- Liest nginx-Log, zählt anonymisierte Unique-IPs (/24 IPv4, /48 IPv6), schreibt in `page_stats`
- **Backfill:** `python3 stats_collector.py --backfill 90`
- **iCal-Feed-Tracking:** `_track_ical_request()` in `services/kalender/routes.py`

---

## Privater Telegram-Bot (services/telegram/routes.py)

Endpunkt `/telegram` – nur Josefs Chat-ID. Token = `TOKEN` aus `/etc/pka/secrets.env`.

| Befehl | Beschreibung |
|--------|--------------|
| `/help` | Alle Befehle |
| `/status` | Server-Status |
| `/sicherheitscheck` | security_check.sh ausführen |
| `/update` | Sicherheitsupdates einspielen |
| `/reboot` | Server neu starten |
| `/pfarrbrief` | Bevorstehende Gottesdienste |
| `/verein` | Alle Vereinstermine |
| `/termine-30` | Nächste 30 Tage |
| `/verkehr <Adresse>` | Verkehrsinfo via Google Directions API |
| `/heimat` | heimat-info.de Import auslösen |
| `/heimat-add <url>` | Neue Gemeinde via Playwright entdecken |
| `/stopp-vko` / `/start-vko` | Wartungsmodus ein/aus |
| *(beliebiger Text)* | → `Todos.json` als `kategorie: pka` |

**Pitfall – Callback-Guard:** Bei `callback_query`-Updates gibt es kein `message`-Objekt → Guard greift nur wenn `not data.get("callback_query")`.
**Pitfall – `send_telegram`:** Signatur `send_telegram(chat_id, text)` – nur 2 Argumente!

---

## Sicherheitsfeatures

- **Admin-Gate:** X-Upload-Token in `sessionStorage`
- **Vereins-Auth:** bcrypt, httponly Cookie `vk_session`, 8h Timeout, Brute-Force-Schutz (5 Versuche → 15 Min.)
- **Multi-Verein-Login:** Eine E-Mail → mehrere Vereine möglich; Pre-Auth-Token (5 min) für Vereinsauswahl
- **DB:** `/opt/rename-webhook/vk_accounts.db` (SQLite WAL) – Tabellen: vereine_accounts, vk_users, vk_sessions, vk_audit, upload_quota, tg_subscriptions, page_stats, ical_feed_requests, ical_feed_vereine
- **Öffentlicher Telegram-Bot:** `@Vereinskalender_bot` – Token `KALENDER_BOT_TOKEN`; Endpunkt `/kalender-bot`
- **E-Mail:** Brevo SMTP (`smtp-relay.brevo.com:587`); `FROM_EMAIL = noreply@vereinskalender.online`

---

## Rename-Service (services/rename/routes.py)

- **529-Retry:** `rename_via_claude()` hat 3-Versuche-Retry (15s / 30s Backoff). Ohne Retry: Overload-Fehler wird geloggt, Cursor trotzdem gesetzt → Datei wird nie erneut versucht.
- **Cursor-Fix nach Stuck:** Datei manuell umbenennen; Cursor lebt weiter.

---

## logbuch_summary.py

- Liest `Logbuch.md` aus Dropbox via Invoice-Dropbox-Token
- Regex matcht `## YYYY-MM-DD` mit beliebigem Suffix
- Mehrere Nachtrag-Einträge werden chronologisch zusammengeführt
- **Nicht im GitHub-Repo** – liegt nur auf Server unter `/opt/rename-webhook/logbuch_summary.py`
- Log prüfen: `tail -20 /var/log/pka-logbuch.log`
