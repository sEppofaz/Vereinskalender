import secrets

from flask import request, session


def get_csrf_token() -> str:
    if "_csrf" not in session:
        session["_csrf"] = secrets.token_hex(32)
    return session["_csrf"]


def validate_csrf() -> bool:
    token = request.form.get("_csrf") or request.headers.get("X-CSRF-Token", "")
    stored = session.get("_csrf", "")
    return bool(token and stored and token == stored)


def csrf_field(token: str) -> str:
    return f'<input type="hidden" name="_csrf" value="{token}">'
