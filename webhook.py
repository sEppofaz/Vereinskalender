from flask import Flask

from services.auth.routes import auth_bp
from services.invoice.routes import invoice_bp
from services.kalender.routes import kalender_bp
from services.kalender_bot.routes import kalender_bot_bp
from services.rename.routes import rename_bp
from services.telegram.routes import telegram_bp
from services.verein.routes import verein_bp


def create_app() -> Flask:
    app = Flask(__name__)
    app.register_blueprint(auth_bp)
    app.register_blueprint(invoice_bp)
    app.register_blueprint(kalender_bp)
    app.register_blueprint(kalender_bot_bp)
    app.register_blueprint(rename_bp)
    app.register_blueprint(telegram_bp)
    app.register_blueprint(verein_bp)
    return app


app = create_app()

if __name__ == '__main__':
    app.run(host='127.0.0.1', port=5000)
