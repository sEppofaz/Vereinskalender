import os
import secrets
from pathlib import Path

from flask import Flask

from services.auth.routes import auth_bp
from services.kalender.routes import kalender_bp
from services.kalender_bot.routes import kalender_bot_bp
from services.rename.routes import rename_bp
from services.telegram.routes import telegram_bp
from services.verein.routes import verein_bp

_SECRET_KEY_FILE = Path("/opt/rename-webhook/flask_secret.key")


def _load_secret_key() -> str:
    env_key = os.environ.get("FLASK_SECRET_KEY", "")
    if env_key:
        return env_key
    if _SECRET_KEY_FILE.exists():
        return _SECRET_KEY_FILE.read_text().strip()
    key = secrets.token_hex(32)
    try:
        _SECRET_KEY_FILE.write_text(key)
    except OSError:
        pass
    return key


def create_app() -> Flask:
    app = Flask(__name__)
    app.secret_key = _load_secret_key()
    app.register_blueprint(auth_bp)
    app.register_blueprint(kalender_bp)
    app.register_blueprint(kalender_bot_bp)
    app.register_blueprint(rename_bp)
    app.register_blueprint(telegram_bp)
    app.register_blueprint(verein_bp)
    return app


app = create_app()

if __name__ == '__main__':
    app.run(host='127.0.0.1', port=5000)
