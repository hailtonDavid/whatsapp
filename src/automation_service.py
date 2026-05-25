"""Orquestração da automação WhatsApp Web (env → browser → envio/grupos)."""

from __future__ import annotations

import asyncio
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from playwright.async_api import BrowserContext, Page, Playwright

from browser_service import initialize_browser, wait_for_login_element
from whatsapp_auto_downloader import (
    AppConfig,
    build_groups_targets_json,
    extract_whatsapp_groups,
    load_app_config,
    load_targets_config,
    open_whatsapp,
    resolve_project_path,
    run_send_once,
    wait_for_whatsapp_ready,
)

_job_lock = asyncio.Lock()


@dataclass
class AutomationBootstrap:
    env_file: Path
    config: AppConfig
    env_loaded: bool


@dataclass
class AutomationSession:
    bootstrap: AutomationBootstrap
    playwright: Playwright
    context: BrowserContext
    page: Page


def load_env_before_browser(env_file: Path) -> AutomationBootstrap:
    """RF01: carrega .env e valida variáveis antes de qualquer inicialização do browser."""
    load_dotenv(env_file, override=True)

    profile_dir = os.getenv("WA_PROFILE_DIR")
    if not profile_dir:
        raise RuntimeError("WA_PROFILE_DIR não definido — carregue o .env antes do browser.")

    config = load_app_config(env_file=env_file)
    return AutomationBootstrap(
        env_file=env_file,
        config=config,
        env_loaded=True,
    )


async def start_automation(env_file: Path) -> AutomationSession:
    """RF05: fluxo completo — .env primeiro, depois Playwright."""
    bootstrap = load_env_before_browser(env_file)
    playwright, context, page = await initialize_browser(bootstrap.config)
    return AutomationSession(
        bootstrap=bootstrap,
        playwright=playwright,
        context=context,
        page=page,
    )


async def detect_qr_code_login(page: Page, timeout_seconds: int = 60) -> bool:
    """RF06: identifica seletor de QR Code / tela de login."""
    await wait_for_login_element(page, timeout_seconds=timeout_seconds)
    return True


def read_last_send_results(app_config: AppConfig) -> list[dict[str, Any]]:
    path = app_config.export_dir / "send" / "last_send.json"
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return []
    return data if isinstance(data, list) else []


def read_groups_inventory(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {
            "ok": False,
            "total_groups": 0,
            "groups": [],
            "inventory_path": str(path),
        }
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {
            "ok": False,
            "total_groups": 0,
            "groups": [],
            "inventory_path": str(path),
            "error": "Arquivo de inventário inválido.",
        }
    if not isinstance(data, dict):
        return {
            "ok": False,
            "total_groups": 0,
            "groups": [],
            "inventory_path": str(path),
            "error": "Formato de inventário inválido.",
        }
    groups = data.get("groups") or []
    data.setdefault("total_groups", len(groups))
    data["inventory_path"] = str(path)
    return data


def read_groups_targets_template(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"targets": [], "targets_path": str(path)}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {"targets": [], "targets_path": str(path), "error": "Arquivo de template inválido."}
    if not isinstance(data, dict):
        return {"targets": [], "targets_path": str(path), "error": "Formato de template inválido."}
    data["targets_path"] = str(path)
    return data


def list_send_targets(targets_path: Path) -> list[dict[str, Any]]:
    config = load_targets_config(targets_path)
    items: list[dict[str, Any]] = []
    for target in config.targets:
        if not target.enabled:
            continue
        items.append(
            {
                "id": target.id,
                "type": target.type,
                "name": target.name,
                "phone": target.phone,
                "send_enabled": target.send_enabled,
                "message": target.message,
            }
        )
    return items


async def execute_send_job(
    env_file: Path,
    targets_path: Path,
    *,
    message: str | None = None,
    target_ids: list[str] | None = None,
    allow_all: bool = False,
    confirm: bool = False,
) -> dict[str, Any]:
    """Executa envio único via Playwright (simulação ou envio real)."""
    if message and not target_ids and not allow_all:
        return {
            "ok": False,
            "error": "Informe target_ids ou allow_all=true ao usar message.",
            "results": [],
        }

    if _job_lock.locked():
        return {
            "ok": False,
            "error": "Já existe um envio ou automação em andamento. Tente novamente em instantes.",
            "busy": True,
            "results": [],
        }

    async with _job_lock:
        bootstrap = load_env_before_browser(env_file)
        targets_config = load_targets_config(targets_path)
        bootstrap.config.export_dir.mkdir(parents=True, exist_ok=True)
        dry_run = not confirm

        if dry_run:
            results = await run_send_once(
                page=None,
                app_config=bootstrap.config,
                targets_config=targets_config,
                message_override=message,
                target_ids=target_ids,
                dry_run=True,
            )
            total_ok = sum(1 for item in results if item.get("ok"))
            return {
                "ok": True,
                "dry_run": True,
                "confirmed": False,
                "total": len(results),
                "successes": total_ok,
                "results": results,
                "summary_path": str(bootstrap.config.export_dir / "send" / "last_send.json"),
            }

        playwright, context, page = await open_whatsapp(bootstrap.config)
        try:
            await wait_for_whatsapp_ready(page, bootstrap.config.ready_timeout)
            results = await run_send_once(
                page=page,
                app_config=bootstrap.config,
                targets_config=targets_config,
                message_override=message,
                target_ids=target_ids,
                dry_run=False,
            )
            await page.wait_for_timeout(5000)
            total_ok = sum(1 for item in results if item.get("ok"))
            return {
                "ok": total_ok > 0,
                "dry_run": False,
                "confirmed": True,
                "total": len(results),
                "successes": total_ok,
                "results": results,
                "summary_path": str(bootstrap.config.export_dir / "send" / "last_send.json"),
            }
        finally:
            await context.close()
            await playwright.stop()


async def execute_list_groups_job(
    env_file: Path,
    *,
    output_path: Path,
    targets_output_path: Path | None = None,
) -> dict[str, Any]:
    """Lista grupos do WhatsApp Web e grava inventário JSON (mesmo fluxo do CLI list-groups)."""
    if _job_lock.locked():
        return {
            "ok": False,
            "error": "Já existe um envio ou automação em andamento. Tente novamente em instantes.",
            "busy": True,
            "total_groups": 0,
            "groups": [],
        }

    async with _job_lock:
        bootstrap = load_env_before_browser(env_file)
        bootstrap.config.export_dir.mkdir(parents=True, exist_ok=True)

        output_path = resolve_project_path(output_path)
        if targets_output_path is not None:
            targets_output_path = resolve_project_path(targets_output_path)

        playwright, context, page = await open_whatsapp(bootstrap.config)
        try:
            await wait_for_whatsapp_ready(page, bootstrap.config.ready_timeout)
            result = await extract_whatsapp_groups(page)

            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_text(
                json.dumps(result, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )

            groups = result.get("groups") or []
            targets_path: str | None = None
            if targets_output_path:
                targets_json = build_groups_targets_json(groups)
                targets_output_path.parent.mkdir(parents=True, exist_ok=True)
                targets_output_path.write_text(
                    json.dumps(targets_json, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
                targets_path = str(targets_output_path)

            return {
                "ok": bool(result.get("ok")) or len(groups) > 0,
                "total_groups": len(groups),
                "groups": groups,
                "generated_at": result.get("generated_at"),
                "diagnostics": result.get("diagnostics"),
                "inventory_path": str(output_path),
                "targets_path": targets_path,
            }
        finally:
            await context.close()
            await playwright.stop()
