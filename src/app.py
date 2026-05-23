"""Interface Flask para automação do WhatsApp Web."""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv
from flask import Flask, jsonify

from whatsapp_auto_downloader import WA_URL, load_app_config

PROJECT_ROOT = Path(__file__).resolve().parents[1]

WHATSAPP_LOGIN_SELECTOR = (
    'canvas[aria-label*="QR" i], '
    'canvas[aria-label*="Scan" i], '
    '[data-testid="link-device-qrcode-alt-linking-help"]'
)


def create_app(env_file: Path | None = None) -> Flask:
    env_path = env_file or (PROJECT_ROOT / ".env")
    load_dotenv(env_path, override=True)

    app = Flask(__name__)
    app.config["ENV_FILE"] = str(env_path)
    app.config["APP_CONFIG"] = load_app_config(env_file=env_path)
    app.config["ENV_LOADED"] = bool(os.getenv("WA_PROFILE_DIR"))

    @app.get("/")
    def index():
        config = app.config["APP_CONFIG"]
        return jsonify(
            {
                "service": "whatsapp-web-automation",
                "status": "ok",
                "whatsapp_url": WA_URL,
                "env_loaded": app.config["ENV_LOADED"],
                "profile_dir": config.profile_dir.name,
            }
        ), 200

    @app.get("/health")
    def health():
        return jsonify({"status": "ok"}), 200

    return app
