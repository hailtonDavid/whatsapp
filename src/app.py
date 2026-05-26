"""Interface Flask para automação do WhatsApp Web."""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv
from flask import Flask, Response, jsonify, redirect, request

from automation_loop import run_automation_coroutine
from automation_service import (
    apply_group_selection,
    apply_group_updates,
    apply_phone_selection,
    apply_phone_updates,
    automation_is_running,
    automation_job_timeout,
    ensure_whatsapp_authorized,
    execute_list_groups_job,
    execute_send_job,
    execute_sync_contacts_job,
    get_held_automation_headless,
    launch_automation,
    list_group_send_targets,
    list_phone_send_targets,
    list_send_targets,
    load_env_before_browser,
    probe_held_session_auth,
    read_contacts_inventory,
    read_groups_inventory,
    read_groups_targets_template,
    read_last_send_results,
    save_uploaded_attachment,
    stop_automation,
)
from wa_selectors import WHATSAPP_LOGIN_SELECTOR
from whatsapp_auto_downloader import WA_URL, load_app_config, resolve_project_path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"
DASHBOARD_TEMPLATE = "dashboard.html"
DEFAULT_TARGETS = "config/targets.json"
DEFAULT_GROUPS_OUTPUT = "exports/groups/groups.json"
DEFAULT_GROUPS_TARGETS = "exports/groups/groups_targets_template.json"
DEFAULT_CONTACTS_OUTPUT = "exports/contacts/contacts.json"
DASHBOARD_PATH = TEMPLATES_DIR / DASHBOARD_TEMPLATE
APP_UI_VERSION = "2026.05.25-painel"


def _load_dashboard_html() -> str:
    if not DASHBOARD_PATH.is_file():
        raise FileNotFoundError(f"Template do painel não encontrado: {DASHBOARD_PATH}")
    return DASHBOARD_PATH.read_text(encoding="utf-8")


def _browser_prefers_html() -> bool:
    accept = request.headers.get("Accept", "")
    return "text/html" in accept and accept.strip().startswith("text/html")


def _parse_send_payload(*, default_targets: str) -> dict:
    payload = request.get_json(silent=True) or {}
    if not isinstance(payload, dict):
        payload = {}

    target_ids = payload.get("target_ids") or payload.get("target_id")
    if isinstance(target_ids, str):
        target_ids = [target_ids]
    elif target_ids is not None and not isinstance(target_ids, list):
        target_ids = [str(target_ids)]

    return {
        "targets": payload.get("targets") or request.args.get("targets") or default_targets,
        "target_ids": target_ids,
        "message": payload.get("message"),
        "attachment": payload.get("attachment") or payload.get("attachment_path"),
        "allow_all": bool(payload.get("allow_all", False)),
        "confirm": bool(payload.get("confirm", False)),
    }


def _parse_groups_payload(*, default_output: str, default_targets: str) -> dict:
    payload = request.get_json(silent=True) or {}
    if not isinstance(payload, dict):
        payload = {}

    targets_output = payload.get("targets_output")
    if targets_output is None and payload.get("generate_targets", True):
        targets_output = default_targets

    merge_targets = payload.get("merge_targets")
    if payload.get("merge_into_config") is False:
        merge_targets = None
    elif merge_targets is None:
        merge_targets = DEFAULT_TARGETS

    resolve_names = payload.get("resolve_names") or payload.get("extra_group_names") or []
    if isinstance(resolve_names, str):
        resolve_names = [line.strip() for line in resolve_names.splitlines() if line.strip()]
    elif not isinstance(resolve_names, list):
        resolve_names = []

    return {
        "output": payload.get("output") or request.args.get("output") or default_output,
        "targets_output": targets_output,
        "merge_targets": merge_targets,
        "resolve_names": [str(name).strip() for name in resolve_names if str(name).strip()],
    }


def _parse_contacts_payload(*, default_output: str, default_targets: str) -> dict:
    payload = request.get_json(silent=True) or {}
    if not isinstance(payload, dict):
        payload = {}

    return {
        "output": payload.get("output") or request.args.get("output") or default_output,
        "targets": payload.get("targets") or request.args.get("targets") or default_targets,
    }


def create_app(
    env_file: Path | None = None,
    *,
    default_targets: str | None = None,
    default_groups_output: str | None = None,
    default_groups_targets: str | None = None,
    default_contacts_output: str | None = None,
) -> Flask:
    env_path = env_file or (PROJECT_ROOT / ".env")
    load_dotenv(env_path, override=True, encoding="utf-8-sig")

    if not DASHBOARD_PATH.is_file():
        raise FileNotFoundError(f"Template do painel não encontrado: {DASHBOARD_PATH}")

    app = Flask(__name__, template_folder=str(TEMPLATES_DIR))
    app.config["ENV_FILE"] = str(env_path)
    app.config["APP_CONFIG"] = load_app_config(env_file=env_path)
    app.config["ENV_LOADED"] = bool(os.getenv("WA_PROFILE_DIR"))
    app.config["DEFAULT_TARGETS"] = default_targets or DEFAULT_TARGETS
    app.config["DEFAULT_GROUPS_OUTPUT"] = default_groups_output or DEFAULT_GROUPS_OUTPUT
    app.config["DEFAULT_GROUPS_TARGETS"] = default_groups_targets or DEFAULT_GROUPS_TARGETS
    app.config["DEFAULT_CONTACTS_OUTPUT"] = default_contacts_output or DEFAULT_CONTACTS_OUTPUT
    app.config["DASHBOARD_HTML"] = _load_dashboard_html()

    def _serve_dashboard():
        return Response(app.config["DASHBOARD_HTML"], mimetype="text/html; charset=utf-8")

    for ui_path in ("/", "/dashboard", "/painel", "/ui"):
        endpoint = f"ui_{ui_path.strip('/') or 'root'}"
        app.add_url_rule(ui_path, endpoint=endpoint, view_func=_serve_dashboard, methods=["GET"])

    @app.get("/api/version")
    def api_version():
        return jsonify(
            {
                "app_ui_version": APP_UI_VERSION,
                "ui_routes": ["/", "/painel", "/dashboard", "/ui"],
                "has_dashboard_template": DASHBOARD_PATH.is_file(),
            }
        ), 200

    @app.get("/api")
    def api_index():
        if _browser_prefers_html() and request.args.get("format") != "json":
            return redirect("/painel", code=302)

        config = app.config["APP_CONFIG"]
        return jsonify(
            {
                "service": "whatsapp-web-automation",
                "status": "ok",
                "whatsapp_url": WA_URL,
                "env_loaded": app.config["ENV_LOADED"],
                "profile_dir": config.profile_dir.name,
                "ui": "/painel",
                "endpoints": {
                    "health": "/health",
                    "automation_status": "/api/automation/status",
                    "automation_ensure_auth": "POST /api/automation/ensure-auth",
                    "automation_start": "POST /api/automation/start",
                    "automation_stop": "POST /api/automation/stop",
                    "send_once": "POST /api/send/once",
                    "send_last": "/api/send/last",
                    "uploads": "POST /api/uploads",
                    "send_targets": "/api/send/targets",
                    "phones_send_targets": "/api/phones/send-targets",
                    "phones_selection": "POST /api/phones/selection",
                    "phones_update": "POST /api/phones/update",
                    "contacts_sync": "POST /api/contacts/sync",
                    "contacts_last": "/api/contacts/last",
                    "groups_generate": "POST /api/groups/generate",
                    "groups_last": "/api/groups/last",
                    "groups_targets_template": "/api/groups/targets-template",
                    "groups_send_targets": "/api/groups/send-targets",
                    "groups_selection": "POST /api/groups/selection",
                    "groups_update": "POST /api/groups/update",
                },
            }
        ), 200

    @app.get("/health")
    def health():
        return jsonify({"status": "ok"}), 200

    @app.get("/api/automation/status")
    def automation_status():
        config = app.config["APP_CONFIG"]
        session_active = automation_is_running()
        session_headless = get_held_automation_headless()
        if session_active:
            automation_state = "running"
        elif app.config["ENV_LOADED"]:
            automation_state = "ready"
        else:
            automation_state = "blocked"

        auth_fields: dict[str, object] = {
            "session_state": None,
            "whatsapp_authorized": None,
        }
        if session_active:
            try:
                auth_fields = run_automation_coroutine(
                    probe_held_session_auth(Path(app.config["ENV_FILE"])),
                    timeout=10,
                )
            except (RuntimeError, TimeoutError):
                auth_fields = {
                    "session_state": "unknown",
                    "whatsapp_authorized": None,
                }

        return jsonify(
            {
                "automation_status": automation_state,
                "session_active": session_active,
                "env_loaded": app.config["ENV_LOADED"],
                "profile_dir": config.profile_dir.name,
                "headless": session_headless if session_headless is not None else config.headless,
                "whatsapp_url": WA_URL,
                **auth_fields,
            }
        ), 200

    @app.post("/api/automation/ensure-auth")
    def automation_ensure_auth():
        if not app.config["ENV_LOADED"]:
            return jsonify(
                {
                    "ok": False,
                    "error": "Variáveis de ambiente não carregadas.",
                }
            ), 400

        config = app.config["APP_CONFIG"]
        try:
            outcome = run_automation_coroutine(
                ensure_whatsapp_authorized(Path(app.config["ENV_FILE"])),
                timeout=45,
            )
        except RuntimeError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400
        except TimeoutError:
            return jsonify(
                {
                    "ok": False,
                    "error": (
                        "Tempo esgotado ao verificar autorização do WhatsApp Web. "
                        "Feche instâncias do Edge e tente novamente."
                    ),
                }
            ), 504

        status_code = 200 if outcome.get("ok") else 403
        return jsonify(
            {
                **outcome,
                "automation_status": "running" if outcome.get("session_active") else "ready",
            }
        ), status_code

    @app.post("/api/automation/start")
    def automation_start():
        if not app.config["ENV_LOADED"]:
            return jsonify(
                {
                    "automation_status": "failed",
                    "error": "Variáveis de ambiente não carregadas.",
                }
            ), 400

        payload = request.get_json(silent=True) or {}
        if not isinstance(payload, dict):
            payload = {}
        launch = bool(payload.get("launch", False))
        visible = bool(payload.get("visible", False))
        headless_override = False if visible else None
        force_relaunch = visible

        try:
            bootstrap = load_env_before_browser(Path(app.config["ENV_FILE"]))
        except RuntimeError as exc:
            return jsonify({"automation_status": "failed", "error": str(exc)}), 400

        config = bootstrap.config

        if launch:
            if automation_is_running() and not force_relaunch:
                return jsonify(
                    {
                        "automation_status": "running",
                        "session_active": True,
                        "error": (
                            "Automação já em execução. "
                            "Use Parar sessão ou Abrir visível (QR Code)."
                        ),
                    }
                ), 409
            try:
                run_automation_coroutine(
                    launch_automation(
                        Path(app.config["ENV_FILE"]),
                        headless=headless_override,
                        force=force_relaunch,
                    ),
                    timeout=float(config.ready_timeout) + 60,
                )
            except RuntimeError as exc:
                return jsonify({"automation_status": "failed", "error": str(exc)}), 400
            except TimeoutError:
                return jsonify(
                    {
                        "automation_status": "failed",
                        "error": (
                            "Tempo esgotado ao abrir o navegador. "
                            "Feche instâncias do Edge e tente novamente."
                        ),
                    }
                ), 504

            active_headless = get_held_automation_headless()
            try:
                auth_fields = run_automation_coroutine(
                    probe_held_session_auth(Path(app.config["ENV_FILE"])),
                    timeout=20,
                )
            except (RuntimeError, TimeoutError):
                auth_fields = {"session_state": "unknown", "whatsapp_authorized": None}

            launch_message: str | None = None
            if auth_fields.get("whatsapp_authorized") is False:
                launch_message = "WhatsApp não autorizado — escaneie o QR Code na janela do Edge."
            elif visible or active_headless is False:
                launch_message = "Navegador visível aberto — escaneie o QR Code na janela do Edge."

            return jsonify(
                {
                    "automation_status": "running" if automation_is_running() else "ready",
                    "session_active": automation_is_running(),
                    "service": "whatsapp-web-automation",
                    "env_loaded": bootstrap.env_loaded,
                    "profile_dir": config.profile_dir.name,
                    "headless": active_headless if active_headless is not None else config.headless,
                    "visible": active_headless is False,
                    "ready_timeout": config.ready_timeout,
                    "whatsapp_url": WA_URL,
                    "login_selector": WHATSAPP_LOGIN_SELECTOR,
                    "message": launch_message,
                    **auth_fields,
                }
            ), 200

        return jsonify(
            {
                "automation_status": "ready",
                "session_active": False,
                "service": "whatsapp-web-automation",
                "env_loaded": bootstrap.env_loaded,
                "profile_dir": config.profile_dir.name,
                "headless": config.headless,
                "ready_timeout": config.ready_timeout,
                "whatsapp_url": WA_URL,
                "login_selector": WHATSAPP_LOGIN_SELECTOR,
            }
        ), 200

    @app.post("/api/automation/stop")
    def automation_stop():
        if not app.config["ENV_LOADED"]:
            return jsonify({"ok": False, "error": "Variáveis de ambiente não carregadas."}), 400

        try:
            outcome = run_automation_coroutine(
                stop_automation(Path(app.config["ENV_FILE"])),
                timeout=120,
            )
        except RuntimeError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400

        return jsonify(
            {
                **outcome,
                "automation_status": "ready" if not outcome.get("session_active") else "running",
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

    @app.get("/api/phones/send-targets")
    def phones_send_targets():
        if not app.config["ENV_LOADED"]:
            return jsonify({"error": "Variáveis de ambiente não carregadas."}), 400

        targets_file = resolve_project_path(
            Path(request.args.get("targets") or app.config["DEFAULT_TARGETS"])
        )
        try:
            items = list_phone_send_targets(targets_file)
        except FileNotFoundError as exc:
            return jsonify({"error": str(exc), "targets": []}), 404

        return jsonify(
            {
                "targets": items,
                "total": len(items),
                "enabled_count": sum(1 for item in items if item.get("enabled")),
                "targets_file": str(targets_file),
            }
        ), 200

    @app.post("/api/phones/selection")
    def phones_selection():
        if not app.config["ENV_LOADED"]:
            return jsonify({"ok": False, "error": "Variáveis de ambiente não carregadas."}), 400

        payload = request.get_json(silent=True) or {}
        if not isinstance(payload, dict):
            payload = {}

        selected_ids = payload.get("selected_ids") or payload.get("target_ids") or []
        if isinstance(selected_ids, str):
            selected_ids = [selected_ids]
        elif not isinstance(selected_ids, list):
            selected_ids = [str(selected_ids)]

        targets_path = resolve_project_path(
            Path(payload.get("targets") or app.config["DEFAULT_TARGETS"])
        )
        default_message = payload.get("message") or payload.get("default_message")

        try:
            outcome = apply_phone_selection(
                targets_path,
                selected_ids,
                default_message=str(default_message) if default_message else None,
            )
        except FileNotFoundError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 404
        except ValueError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 422

        return jsonify(outcome), 200

    @app.post("/api/phones/update")
    def phones_update():
        if not app.config["ENV_LOADED"]:
            return jsonify({"ok": False, "error": "Variáveis de ambiente não carregadas."}), 400

        payload = request.get_json(silent=True) or {}
        if not isinstance(payload, dict):
            payload = {}

        updates = payload.get("updates") or payload.get("items") or []
        if not isinstance(updates, list):
            return jsonify({"ok": False, "error": "Campo updates deve ser uma lista."}), 422

        targets_path = resolve_project_path(
            Path(payload.get("targets") or app.config["DEFAULT_TARGETS"])
        )

        try:
            outcome = apply_phone_updates(targets_path, updates)
        except FileNotFoundError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 404
        except ValueError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 422

        return jsonify(outcome), 200

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

    @app.post("/api/uploads")
    def uploads_create():
        if not app.config["ENV_LOADED"]:
            return jsonify({"ok": False, "error": "Variáveis de ambiente não carregadas."}), 400

        uploaded = request.files.get("file")
        if uploaded is None or not uploaded.filename:
            return jsonify({"ok": False, "error": "Envie um arquivo no campo file."}), 422

        try:
            outcome = save_uploaded_attachment(
                app.config["APP_CONFIG"],
                uploaded.filename,
                uploaded.read(),
            )
        except ValueError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 422

        return jsonify(outcome), 200

    @app.post("/api/send/once")
    def send_once():
        if not app.config["ENV_LOADED"]:
            return jsonify({"ok": False, "error": "Variáveis de ambiente não carregadas."}), 400

        data = _parse_send_payload(default_targets=app.config["DEFAULT_TARGETS"])
        targets_file = resolve_project_path(Path(data["targets"]))

        try:
            config = app.config["APP_CONFIG"]
            outcome = run_automation_coroutine(
                execute_send_job(
                    Path(app.config["ENV_FILE"]),
                    targets_file,
                    message=data["message"],
                    target_ids=data["target_ids"],
                    allow_all=data["allow_all"],
                    confirm=data["confirm"],
                    attachment=data["attachment"],
                ),
                timeout=automation_job_timeout(config, heavy=False),
            )
        except FileNotFoundError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 404
        except RuntimeError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400
        except TimeoutError:
            return jsonify({"ok": False, "error": "Tempo esgotado ao executar envio."}), 504

        if outcome.get("busy"):
            return jsonify(outcome), 409
        if outcome.get("requires_qr"):
            return jsonify(outcome), 403

        status = 200 if outcome.get("ok") else 422
        if outcome.get("dry_run"):
            status = 200
        return jsonify(outcome), status

    @app.get("/api/groups/send-targets")
    def groups_send_targets():
        if not app.config["ENV_LOADED"]:
            return jsonify({"error": "Variáveis de ambiente não carregadas."}), 400

        targets_path = resolve_project_path(
            Path(request.args.get("targets_output") or app.config["DEFAULT_GROUPS_TARGETS"])
        )
        try:
            items = list_group_send_targets(targets_path)
        except FileNotFoundError as exc:
            return jsonify({"error": str(exc), "targets": []}), 404

        return jsonify(
            {
                "targets": items,
                "total": len(items),
                "enabled_count": sum(1 for item in items if item.get("enabled")),
                "targets_path": str(targets_path),
            }
        ), 200

    @app.post("/api/groups/selection")
    def groups_selection():
        if not app.config["ENV_LOADED"]:
            return jsonify({"ok": False, "error": "Variáveis de ambiente não carregadas."}), 400

        payload = request.get_json(silent=True) or {}
        if not isinstance(payload, dict):
            payload = {}

        selected_ids = payload.get("selected_ids") or payload.get("target_ids") or []
        if isinstance(selected_ids, str):
            selected_ids = [selected_ids]
        elif not isinstance(selected_ids, list):
            selected_ids = [str(selected_ids)]

        targets_path = resolve_project_path(
            Path(payload.get("targets_output") or app.config["DEFAULT_GROUPS_TARGETS"])
        )
        default_message = payload.get("message") or payload.get("default_message")

        try:
            outcome = apply_group_selection(
                targets_path,
                selected_ids,
                default_message=str(default_message) if default_message else None,
            )
        except FileNotFoundError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 404
        except ValueError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 422

        return jsonify(outcome), 200

    @app.post("/api/groups/update")
    def groups_update():
        if not app.config["ENV_LOADED"]:
            return jsonify({"ok": False, "error": "Variáveis de ambiente não carregadas."}), 400

        payload = request.get_json(silent=True) or {}
        if not isinstance(payload, dict):
            payload = {}

        updates = payload.get("updates") or payload.get("items") or []
        if not isinstance(updates, list):
            return jsonify({"ok": False, "error": "Campo updates deve ser uma lista."}), 422

        targets_path = resolve_project_path(
            Path(payload.get("targets_output") or payload.get("targets") or app.config["DEFAULT_GROUPS_TARGETS"])
        )

        try:
            outcome = apply_group_updates(targets_path, updates)
        except FileNotFoundError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 404
        except ValueError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 422

        return jsonify(outcome), 200

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

        data = _parse_groups_payload(
            default_output=app.config["DEFAULT_GROUPS_OUTPUT"],
            default_targets=app.config["DEFAULT_GROUPS_TARGETS"],
        )
        output_path = Path(data["output"])
        targets_output = Path(data["targets_output"]) if data["targets_output"] else None
        merge_targets = Path(data["merge_targets"]) if data.get("merge_targets") else None

        try:
            config = app.config["APP_CONFIG"]
            outcome = run_automation_coroutine(
                execute_list_groups_job(
                    Path(app.config["ENV_FILE"]),
                    output_path=output_path,
                    targets_output_path=targets_output,
                    merge_targets_path=merge_targets,
                    resolve_names=data.get("resolve_names") or [],
                ),
                timeout=automation_job_timeout(config, heavy=True),
            )
        except RuntimeError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400
        except TimeoutError:
            return jsonify({"ok": False, "error": "Tempo esgotado ao gerar lista de grupos."}), 504

        if outcome.get("busy"):
            return jsonify(outcome), 409
        if outcome.get("requires_qr"):
            return jsonify(outcome), 403

        status = 200 if outcome.get("ok") else 422
        return jsonify(outcome), status

    @app.get("/api/contacts/last")
    def contacts_last():
        inventory_path = resolve_project_path(
            Path(request.args.get("output") or app.config["DEFAULT_CONTACTS_OUTPUT"])
        )
        data = read_contacts_inventory(inventory_path)
        return jsonify(data), 200

    @app.post("/api/contacts/sync")
    def contacts_sync():
        if not app.config["ENV_LOADED"]:
            return jsonify({"ok": False, "error": "Variáveis de ambiente não carregadas."}), 400

        data = _parse_contacts_payload(
            default_output=app.config["DEFAULT_CONTACTS_OUTPUT"],
            default_targets=app.config["DEFAULT_TARGETS"],
        )

        try:
            config = app.config["APP_CONFIG"]
            outcome = run_automation_coroutine(
                execute_sync_contacts_job(
                    Path(app.config["ENV_FILE"]),
                    output_path=Path(data["output"]),
                    targets_path=Path(data["targets"]),
                ),
                timeout=automation_job_timeout(config, heavy=True),
            )
        except RuntimeError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400
        except ValueError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 422
        except TimeoutError:
            return jsonify({"ok": False, "error": "Tempo esgotado ao sincronizar contatos."}), 504

        if outcome.get("busy"):
            return jsonify(outcome), 409
        if outcome.get("requires_qr"):
            return jsonify(outcome), 403

        status = 200 if outcome.get("ok") else 422
        return jsonify(outcome), status

    return app


def main() -> None:
    import threading
    import webbrowser

    host = os.getenv("FLASK_HOST", "127.0.0.1")
    port = int(os.getenv("FLASK_PORT", "5014"))
    debug = os.getenv("FLASK_DEBUG", "false").strip().lower() in {"1", "true", "yes", "sim"}
    open_browser = os.getenv("FLASK_OPEN_BROWSER", "true").strip().lower() in {
        "1",
        "true",
        "yes",
        "sim",
    }

    panel_url = f"http://{host}:{port}/painel"
    api_url = f"http://{host}:{port}/api?format=json"

    if open_browser:
        threading.Timer(2.5, lambda: webbrowser.open(panel_url)).start()

    print("")
    print("=" * 56)
    print("  WhatsApp Web Automation — painel iniciado")
    print("=" * 56)
    print(f"  PAINEL (interface):  {panel_url}")
    print(f"  Versao do painel:    {APP_UI_VERSION}")
    print(f"  API (JSON):          {api_url}")
    print("  Pressione CTRL+C para encerrar.")
    print("=" * 56)
    print("")

    create_app().run(host=host, port=port, debug=debug)


if __name__ == "__main__":
    main()
