import json
import secrets
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path

DB_FILE = Path("/opt/rename-webhook/vk_accounts.db")
SESSION_TIMEOUT_HOURS = 8


@contextmanager
def db_conn():
    conn = sqlite3.connect(DB_FILE, check_same_thread=False, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db():
    # Migration: UNIQUE-Constraint auf email entfernen (eine E-Mail → mehrere Vereine)
    with db_conn() as conn:
        schema = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='vk_users'"
        ).fetchone()
        if schema and "UNIQUE NOT NULL" in schema["sql"] and "email" in schema["sql"]:
            existing_cols = [r["name"] for r in conn.execute("PRAGMA table_info(vk_users)").fetchall()]
            name_sel   = "name"    if "name"    in existing_cols else "NULL"
            telefon_sel = "telefon" if "telefon" in existing_cols else "NULL"
            conn.executescript(f"""
                PRAGMA foreign_keys=OFF;
                CREATE TABLE vk_users_new (
                    id                    INTEGER PRIMARY KEY,
                    email                 TEXT NOT NULL,
                    password_hash         TEXT NOT NULL,
                    verein_id             INTEGER NOT NULL REFERENCES vereine_accounts(id),
                    role                  TEXT NOT NULL DEFAULT 'admin',
                    aktiv                 BOOLEAN DEFAULT 1,
                    created_at            DATETIME DEFAULT CURRENT_TIMESTAMP,
                    einladungs_token      TEXT,
                    einladungs_expires    DATETIME,
                    reset_token           TEXT,
                    reset_token_expires   DATETIME,
                    email_verified        BOOLEAN DEFAULT 0,
                    verify_token          TEXT,
                    verify_token_expires  DATETIME,
                    totp_secret           TEXT,
                    totp_recovery_hashes  TEXT,
                    login_attempts        INTEGER DEFAULT 0,
                    locked_until          DATETIME,
                    name                  TEXT,
                    telefon               TEXT
                );
                INSERT INTO vk_users_new
                    SELECT id, email, password_hash, verein_id, role, aktiv, created_at,
                           einladungs_token, einladungs_expires, reset_token, reset_token_expires,
                           email_verified, verify_token, verify_token_expires, totp_secret,
                           totp_recovery_hashes, login_attempts, locked_until,
                           {name_sel}, {telefon_sel}
                    FROM vk_users;
                DROP TABLE vk_users;
                ALTER TABLE vk_users_new RENAME TO vk_users;
                PRAGMA foreign_keys=ON;
            """)

    with db_conn() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS vereine_accounts (
                id                  INTEGER PRIMARY KEY,
                verein_key          TEXT UNIQUE,
                verein_name         TEXT NOT NULL,
                status              TEXT NOT NULL DEFAULT 'pending',
                created_at          DATETIME DEFAULT CURRENT_TIMESTAMP,
                freigegeben_at      DATETIME,
                selbstverpflichtung BOOLEAN DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS vk_users (
                id                    INTEGER PRIMARY KEY,
                email                 TEXT NOT NULL,
                password_hash         TEXT NOT NULL,
                verein_id             INTEGER NOT NULL REFERENCES vereine_accounts(id),
                role                  TEXT NOT NULL DEFAULT 'admin',
                aktiv                 BOOLEAN DEFAULT 1,
                created_at            DATETIME DEFAULT CURRENT_TIMESTAMP,
                einladungs_token      TEXT,
                einladungs_expires    DATETIME,
                reset_token           TEXT,
                reset_token_expires   DATETIME,
                email_verified        BOOLEAN DEFAULT 0,
                verify_token          TEXT,
                verify_token_expires  DATETIME,
                totp_secret           TEXT,
                totp_recovery_hashes  TEXT,
                login_attempts        INTEGER DEFAULT 0,
                locked_until          DATETIME,
                name                  TEXT,
                telefon               TEXT
            );

            CREATE TABLE IF NOT EXISTS vk_sessions (
                id           TEXT PRIMARY KEY,
                user_id      INTEGER NOT NULL REFERENCES vk_users(id),
                created_at   DATETIME DEFAULT CURRENT_TIMESTAMP,
                last_active  DATETIME DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS vk_audit (
                id          INTEGER PRIMARY KEY,
                aktion      TEXT NOT NULL,
                termin_id   TEXT NOT NULL,
                verein_key  TEXT NOT NULL,
                user_id     INTEGER REFERENCES vk_users(id),
                timestamp   DATETIME DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS upload_quota (
                verein_id INTEGER NOT NULL,
                datum     TEXT    NOT NULL,
                count     INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (verein_id, datum)
            );

            CREATE TABLE IF NOT EXISTS tg_subscriptions (
                chat_id    TEXT NOT NULL,
                verein_key TEXT NOT NULL,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (chat_id, verein_key)
            );

            CREATE TABLE IF NOT EXISTS page_stats (
                datum           TEXT PRIMARY KEY,
                views           INTEGER NOT NULL DEFAULT 0,
                unique_visitors INTEGER NOT NULL DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS ical_feed_requests (
                date    TEXT NOT NULL,
                ip_hash TEXT NOT NULL,
                PRIMARY KEY (date, ip_hash)
            );
        """)
        # Migrations: neue Spalten (scheitern lautlos wenn bereits vorhanden)
        for col_sql in [
            "ALTER TABLE vk_users ADD COLUMN name TEXT",
            "ALTER TABLE vk_users ADD COLUMN telefon TEXT",
            "ALTER TABLE vereine_accounts ADD COLUMN rubrik TEXT NOT NULL DEFAULT 'Verein'",
            "ALTER TABLE vereine_accounts ADD COLUMN heimatort TEXT",
            "ALTER TABLE vereine_accounts ADD COLUMN plz TEXT",
            "ALTER TABLE vereine_accounts ADD COLUMN gemeinde TEXT",
            "ALTER TABLE vereine_accounts ADD COLUMN landkreis TEXT",
        ]:
            try:
                conn.execute(col_sql)
            except Exception:
                pass


def create_session(user_id: int) -> str:
    token = secrets.token_hex(32)
    with db_conn() as conn:
        conn.execute(
            "INSERT INTO vk_sessions (id, user_id) VALUES (?, ?)",
            (token, user_id),
        )
    return token


def get_session_user(token: str) -> dict | None:
    if not token:
        return None
    cutoff = (datetime.utcnow() - timedelta(hours=SESSION_TIMEOUT_HOURS)).strftime("%Y-%m-%d %H:%M:%S")
    with db_conn() as conn:
        row = conn.execute(
            """SELECT u.id, u.email, u.role, u.aktiv,
                      u.email_verified, u.totp_secret,
                      v.id as verein_id, v.verein_key, v.verein_name, v.status as verein_status
               FROM vk_sessions s
               JOIN vk_users u ON u.id = s.user_id
               JOIN vereine_accounts v ON v.id = u.verein_id
               WHERE s.id = ? AND s.last_active > ?""",
            (token, cutoff),
        ).fetchone()
        if row:
            conn.execute(
                "UPDATE vk_sessions SET last_active = CURRENT_TIMESTAMP WHERE id = ?",
                (token,),
            )
        return dict(row) if row else None


def delete_session(token: str):
    with db_conn() as conn:
        conn.execute("DELETE FROM vk_sessions WHERE id = ?", (token,))


def log_audit(aktion: str, termin_id: str, verein_key: str, user_id: int):
    with db_conn() as conn:
        conn.execute(
            "INSERT INTO vk_audit (aktion, termin_id, verein_key, user_id) VALUES (?,?,?,?)",
            (aktion, termin_id, verein_key, user_id),
        )


def get_upload_count(verein_id: int, datum: str) -> int:
    with db_conn() as conn:
        row = conn.execute(
            "SELECT count FROM upload_quota WHERE verein_id=? AND datum=?",
            (verein_id, datum),
        ).fetchone()
        return row["count"] if row else 0


def increment_upload_quota(verein_id: int, datum: str):
    with db_conn() as conn:
        conn.execute(
            """INSERT INTO upload_quota (verein_id, datum, count) VALUES (?,?,1)
               ON CONFLICT(verein_id, datum) DO UPDATE SET count = count + 1""",
            (verein_id, datum),
        )


def tg_subscribe(chat_id: str, verein_key: str) -> bool:
    with db_conn() as conn:
        existing = conn.execute(
            "SELECT 1 FROM tg_subscriptions WHERE chat_id=? AND verein_key=?",
            (chat_id, verein_key)
        ).fetchone()
        if existing:
            return False
        conn.execute(
            "INSERT INTO tg_subscriptions (chat_id, verein_key) VALUES (?,?)",
            (chat_id, verein_key)
        )
        return True


def tg_unsubscribe(chat_id: str, verein_key: str) -> bool:
    with db_conn() as conn:
        result = conn.execute(
            "DELETE FROM tg_subscriptions WHERE chat_id=? AND verein_key=?",
            (chat_id, verein_key)
        )
        return result.rowcount > 0


def tg_unsubscribe_all(chat_id: str) -> int:
    with db_conn() as conn:
        result = conn.execute(
            "DELETE FROM tg_subscriptions WHERE chat_id=?", (chat_id,)
        )
        return result.rowcount


def tg_get_subscriptions(chat_id: str) -> list[str]:
    with db_conn() as conn:
        rows = conn.execute(
            "SELECT verein_key FROM tg_subscriptions WHERE chat_id=? ORDER BY verein_key",
            (chat_id,)
        ).fetchall()
        return [r["verein_key"] for r in rows]


def tg_get_subscribers_for_verein(verein_key: str) -> list[str]:
    with db_conn() as conn:
        rows = conn.execute(
            "SELECT chat_id FROM tg_subscriptions WHERE verein_key=?", (verein_key,)
        ).fetchall()
        return [r["chat_id"] for r in rows]


def tg_get_all_subscriptions() -> list[dict]:
    with db_conn() as conn:
        rows = conn.execute(
            "SELECT chat_id, verein_key FROM tg_subscriptions"
        ).fetchall()
        return [dict(r) for r in rows]


def get_page_stats(from_date: str, to_date: str) -> list[dict]:
    with db_conn() as conn:
        rows = conn.execute(
            "SELECT datum, views, unique_visitors FROM page_stats "
            "WHERE datum >= ? AND datum <= ? ORDER BY datum",
            (from_date, to_date),
        ).fetchall()
        return [dict(r) for r in rows]
