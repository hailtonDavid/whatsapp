"""Interface Flask para automação do WhatsApp Web."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

from dotenv import load_dotenv
from flask import Flask, jsonify, request

from automation_service import (
    execute_list_groups_job,
    execute_send_job,
    list_send_targets,
    load_env_before_browser,
    read_groups_inventory,
    read_groups_targets_template,
    read_last_send_results,
)
from wa_selectors import WHATSAPP_LOGIN_SELECTOR
from whatsapp_auto_downloader import WA_URL, load_app_config, resolve_project_path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_TARGETS = "config/targets.json"
DEFAULT_GROUPS_OUTPUT = "exports/groups/groups.json"
DEFAULT_GROUPS_TARGETS = "exports/groups/groups_targets_template.json"


def _parse_send_payload() -> dict:
    payload = request.get_json(silent=True) or {}
    if not isinstance(payload, dict):
        payload = {}

    target_ids = payload.get("target_ids") or payload.get("target_id")
    if isinstance(target_ids, str):
        target_ids = [target_ids]
    elif target_ids is not None and not isinstance(target_ids, list):
        target_ids = [str(target_ids)]

    return {
        "targets": payload.get("targets") or request.args.get("targets") or DEFAULT_TARGETS,
        "target_ids": target_ids,
        "message": payload.get("message"),
        "allow_all": bool(payload.get("allow_all", False)),
        "confirm": bool(payload.get("confirm", False)),
    }


def _parse_groups_payload() -> dict:
    payload = request.get_json(silent=True) or {}
    if not isinstance(payload, dict):
        payload = {}

    targets_output = payload.get("targets_output")
    if targets_output is None and payload.get("generate_targets", True):
        targets_output = DEFAULT_GROUPS_TARGETS

    return {
        "output": payload.get("output") or request.args.get("output") or DEFAULT_GROUPS_OUTPUT,
        "targets_output": targets_output,
    }


def create_app(env_file: Path | None = None) -> Flask:
    env_path = env_file or (PROJECT_ROOT / ".env")
    load_dotenv(env_path, override=True)

    app = Flask(__name__)
    app.config["ENV_FILE"] = str(env_path)
    app.config["APP_CONFIG"] = load_app_config(env_file=env_path)
    app.config["ENV_LOADED"] = bool(os.getenv("WA_PROFILE_DIR"))
    app.config["DEFAULT_TARGETS"] = DEFAULT_TARGETS
    app.config["DEFAULT_GROUPS_OUTPUT"] = DEFAULT_GROUPS_OUTPUT
    app.config["DEFAULT_GROUPS_TARGETS"] = DEFAULT_GROUPS_TARGETS

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
                "endpoints": {
                    "health": "/health",
                    "automation_status": "/api/automation/status",
                    "automation_start": "POST /api/automation/start",
                    "send_once": "POST /api/send/once",
                    "send_last": "/api/send/last",
                    "send_targets": "/api/send/targets",
                    "groups_generate": "POST /api/groups/generate",
                    "groups_last": "/api/groups/last",
                    "groups_targets_template": "/api/groups/targets-template",
                },
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

    @app.get("/api/send/targets")
    def send_targets():
        if not app.config["ENV_LOADED"]:
            return jsonify({"error": "Variáveis de ambiente não carregadas."}), 400

        targets_file = resolve_project_path(
            Path(request.args.get("targets") or app.config["DEFAULT_TARGETS"])
        )
        try:
            items = list_send_targets(targets_file)
        except FileNotFoundError as exc:
            return jsonify({"error": str(exc)}), 404

        return jsonify({"targets": items, "targets_file": str(targets_file)}), 200

    @app.get("/api/send/last")
    def send_last():
        config = app.config["APP_CONFIG"]
        results = read_last_send_results(config)
        return jsonify(
            {
                "total": len(results),
                "successes": sum(1 for item in results if item.get("ok")),
                "results": results,
                "summary_path": str(config.export_dir / "send" / "last_send.json"),
            }
        ), 200

    @app.post("/api/send/once")
    def send_once():
        if not app.config["ENV_LOADED"]:
            return jsonify({"ok": False, "error": "Variáveis de ambiente não carregadas."}), 400

        data = _parse_send_payload()
        targets_file = resolve_project_path(Path(data["targets"]))

        try:
            outcome = asyncio.run(
                execute_send_job(
                    Path(app.config["ENV_FILE"]),
                    targets_file,
                    message=data["message"],
                    target_ids=data["target_ids"],
                    allow_all=data["allow_all"],
                    confirm=data["confirm"],
                )
            )
        except FileNotFoundError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 404
        except RuntimeError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400

        if outcome.get("busy"):
            return jsonify(outcome), 409

        status = 200 if outcome.get("ok") else 422
        if outcome.get("dry_run"):
            status = 200
        return jsonify(outcome), status

    @app.get("/api/groups/last")
    def groups_last():
        inventory_path = resolve_project_path(
            Path(request.args.get("output") or app.config["DEFAULT_GROUPS_OUTPUT"])
        )
        data = read_groups_inventory(inventory_path)
        return jsonify(data), 200

    @app.get("/api/groups/targets-template")
    def groups_targets_template():
        targets_path = resolve_project_path(
            Path(request.args.get("targets_output") or app.config["DEFAULT_GROUPS_TARGETS"])
        )
        data = read_groups_targets_template(targets_path)
        status = 200 if "error" not in data else 404
        return jsonify(data), status

    @app.post("/api/groups/generate")
    def groups_generate():
        if not app.config["ENV_LOADED"]:
            return jsonify({"ok": False, "error": "Variáveis de ambiente não carregadas."}), 400

        data = _parse_groups_payload()
        output_path = Path(data["output"])
        targets_output = Path(data["targets_output"]) if data["targets_output"] else None

        try:
            outcome = asyncio.run(
                execute_list_groups_job(
                    Path(app.config["ENV_FILE"]),
                    output_path=output_path,
                    targets_output_path=targets_output,
                )
            )
        except RuntimeError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400

        if outcome.get("busy"):
            return jsonify(outcome), 409

        status = 200 if outcome.get("ok") else 422
        return jsonify(outcome), status

    return app


def main() -> None:
    host = os.getenv("FLASK_HOST", "127.0.0.1")
    port = int(os.getenv("FLASK_PORT", "5000"))
    debug = os.getenv("FLASK_DEBUG", "false").strip().lower() in {"1", "true", "yes", "sim"}
    create_app().run(host=host, port=port, debug=debug)


if __name__ == "__main__":
    main()
