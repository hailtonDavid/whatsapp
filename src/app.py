"""Interface Flask para automação do WhatsApp Web."""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv
from flask import Flask, jsonify

from automation_service import load_env_before_browser
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

    @app.get("/api/automation/status")
    def automation_status():
        config = app.config["APP_CONFIG"]
        return jsonify(
            {
                "automation_status": "ready" if app.config["ENV_LOADED"] else "blocked",
                "env_loaded": app.config["ENV_LOADED"],
                "profile_dir": config.profile_dir.name,
                "headless": config.headless,
                "whatsapp_url": WA_URL,
            }
        ), 200

    @app.post("/api/automation/start")
    def automation_start():
        if not app.config["ENV_LOADED"]:
            return jsonify(
                {
                    "automation_status": "failed",
                    "error": "Variáveis de ambiente não carregadas.",
                }
            ), 400

        try:
            bootstrap = load_env_before_browser(Path(app.config["ENV_FILE"]))
        except RuntimeError as exc:
            return jsonify({"automation_status": "failed", "error": str(exc)}), 400

        config = bootstrap.config
        return jsonify(
            {
                "automation_status": "ready",
                "service": "whatsapp-web-automation",
                "env_loaded": bootstrap.env_loaded,
                "profile_dir": config.profile_dir.name,
                "headless": config.headless,
                "ready_timeout": config.ready_timeout,
                "whatsapp_url": WA_URL,
                "login_selector": WHATSAPP_LOGIN_SELECTOR,
            }
        ), 200

    return app
