import logging
import os

import yfinance as yf
from flask import Flask

from . import config
from .db import init_db
from .routes.api import api_bp
from .routes.web import web_bp
from .security import get_csrf_token
from .services.set100 import maybe_auto_sync_set100


def create_app():
    template_dir = os.path.join(os.path.dirname(__file__), "..", "templates")
    app = Flask(__name__, template_folder=template_dir)

    app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "gg-epsprov2-dev-secret")
    app.logger.setLevel(logging.INFO)

    app.config["IS_VERCEL"] = os.environ.get("VERCEL") == "1"
    app.config["DB_FILE_NAME"] = config.DB_FILE_NAME
    app.config["STOCKS_FILE_NAME"] = config.STOCKS_FILE_NAME
    app.config["CACHE_DIR_NAME"] = config.CACHE_DIR_NAME
    app.config["API_MAX_WORKERS"] = config.API_MAX_WORKERS

    app.config["SET100_AUTO_SYNC_INTERVAL_SECONDS"] = config.SET100_AUTO_SYNC_INTERVAL_SECONDS
    app.config["SET100_MIN_EXPECTED_SYMBOLS"] = config.SET100_MIN_EXPECTED_SYMBOLS

    app.config["GRADE_A_MIN_SCORE"] = config.GRADE_A_MIN_SCORE
    app.config["SNIPER_MIN_MOS"] = config.SNIPER_MIN_MOS

    app.config["ENABLE_TRANSLATION"] = config.ENABLE_TRANSLATION
    app.config["TRANSLATION_MAX_CHARS"] = config.TRANSLATION_MAX_CHARS

    cache_dir = config.get_cache_dir(app.config["IS_VERCEL"], app.config["CACHE_DIR_NAME"])
    try:
        os.makedirs(cache_dir, exist_ok=True)
        yf.set_tz_cache_location(cache_dir)
    except Exception as exc:
        app.logger.warning("Unable to initialize yfinance cache directory: %s", exc)

    @app.before_request
    def initialize():
        get_csrf_token()
        if not getattr(app, "db_initialized", False):
            init_db()
            app.db_initialized = True
        maybe_auto_sync_set100()

    app.register_blueprint(web_bp)
    app.register_blueprint(api_bp)

    return app

