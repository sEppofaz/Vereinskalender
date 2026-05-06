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
                email                 TEXT UNIQUE NOT NULL,
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
                locked_until          DATETIME
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
        """)


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
    cutoff = (datetime.utcnow() - timedelta(hours=SESSION_TIMEOUT_HOURS)).isoformat()
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
