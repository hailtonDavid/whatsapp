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
from playwright_lifecycle import drain_event_loop_subprocesses, shutdown_playwright_stack
from whatsapp_auto_downloader import (
    AppConfig,
    build_groups_targets_json,
    extract_whatsapp_contacts,
    extract_whatsapp_groups,
    list_group_names_from_targets_file,
    load_app_config,
    load_targets_config,
    merge_contacts_into_targets,
    merge_groups_into_targets,
    normalize_phone_digits,
    open_whatsapp,
    resolve_project_path,
    run_send_once,
    safe_id,
    wait_for_whatsapp_ready,
)

_job_lock = asyncio.Lock()
_held_session: AutomationSession | None = None


def automation_is_running() -> bool:
    return _held_session is not None


def get_held_automation_headless() -> bool | None:
    """Headless da sessão ativa, ou None se não houver sessão."""
    if _held_session is None:
        return None
    return _held_session.bootstrap.config.headless


def reset_held_automation_session_for_tests() -> None:
    """Limpa ponteiro de sessão — apenas para isolamento entre testes."""
    global _held_session
    _held_session = None


async def shutdown_automation_session(session: AutomationSession) -> None:
    await shutdown_playwright_stack(
        pages=[session.page],
        context=session.context,
        playwright=session.playwright,
    )


async def launch_automation(
    env_file: Path,
    *,
    headless: bool | None = None,
    force: bool = False,
) -> AutomationSession:
    """Abre sessão Playwright persistente e mantém referência até stop."""
    global _held_session
    if _held_session is not None:
        if not force:
            raise RuntimeError("Automação já está em execução.")
        await shutdown_automation_session(_held_session)
        _held_session = None
        await drain_event_loop_subprocesses()

    session = await start_automation(env_file, headless=headless)
    _held_session = session
    return session


async def stop_automation() -> dict[str, Any]:
    """Encerra sessão Playwright ativa, se houver."""
    global _held_session
    if _held_session is None:
        return {"ok": True, "session_active": False, "stopped": False}

    session = _held_session
    _held_session = None
    await shutdown_automation_session(session)
    return {"ok": True, "session_active": False, "stopped": True}


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
    load_dotenv(env_file, override=True, encoding="utf-8-sig")

    profile_dir = os.getenv("WA_PROFILE_DIR")
    if not profile_dir:
        raise RuntimeError("WA_PROFILE_DIR não definido — carregue o .env antes do browser.")

    config = load_app_config(env_file=env_file)
    return AutomationBootstrap(
        env_file=env_file,
        config=config,
        env_loaded=True,
    )


async def start_automation(env_file: Path, *, headless: bool | None = None) -> AutomationSession:
    """RF05: fluxo completo — .env primeiro, depois Playwright."""
    bootstrap = load_env_before_browser(env_file)
    if headless is not None:
        bootstrap.config.headless = headless
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


def read_contacts_inventory(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {
            "ok": False,
            "total_contacts": 0,
            "contacts": [],
            "inventory_path": str(path),
        }
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {
            "ok": False,
            "total_contacts": 0,
            "contacts": [],
            "inventory_path": str(path),
            "error": "Arquivo de inventário inválido.",
        }
    if not isinstance(data, dict):
        return {
            "ok": False,
            "total_contacts": 0,
            "contacts": [],
            "inventory_path": str(path),
            "error": "Formato de inventário inválido.",
        }
    contacts = data.get("contacts") or []
    data.setdefault("total_contacts", len(contacts))
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


def list_group_send_targets(targets_path: Path) -> list[dict[str, Any]]:
    """Lista todos os grupos do template de inventário para seleção na UI."""
    data = read_groups_targets_template(targets_path)
    items: list[dict[str, Any]] = []
    for raw in data.get("targets") or []:
        if str(raw.get("type", "")).strip().lower() != "group":
            continue
        send_block = raw.get("send") if isinstance(raw.get("send"), dict) else {}
        metadata = raw.get("metadata") if isinstance(raw.get("metadata"), dict) else {}
        items.append(
            {
                "id": raw.get("id"),
                "type": "group",
                "name": raw.get("name"),
                "enabled": bool(raw.get("enabled", False)),
                "send_enabled": bool(send_block.get("enabled", False)),
                "message": send_block.get("message") or "",
                "whatsapp_id": metadata.get("whatsapp_id"),
            }
        )
    items.sort(key=lambda item: (item.get("name") or item.get("id") or "").casefold())
    return items


def list_phone_send_targets(targets_path: Path) -> list[dict[str, Any]]:
    """Lista todos os números (type=phone) do targets.json para seleção na UI."""
    config = load_targets_config(targets_path)
    items: list[dict[str, Any]] = []
    for target in config.targets:
        if target.type != "phone":
            continue
        items.append(
            {
                "id": target.id,
                "type": "phone",
                "phone": target.phone,
                "name": target.name,
                "enabled": target.enabled,
                "send_enabled": target.send_enabled,
                "message": target.message or "",
            }
        )
    items.sort(key=lambda item: (item.get("phone") or item.get("id") or "").casefold())
    return items


def apply_phone_updates(
    targets_path: Path,
    updates: list[dict[str, Any]],
) -> dict[str, Any]:
    """Atualiza telefone, mensagem e/ou enabled de alvos type=phone."""
    if not targets_path.exists():
        raise FileNotFoundError(f"Arquivo de alvos não encontrado: {targets_path}")

    data = json.loads(targets_path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("Formato de alvos inválido.")

    patch_by_id: dict[str, dict[str, Any]] = {}
    for raw in updates:
        if not isinstance(raw, dict):
            continue
        target_id = safe_id(str(raw.get("id") or ""))
        if target_id:
            patch_by_id[target_id] = raw

    changed = 0
    for raw in data.get("targets") or []:
        if str(raw.get("type", "")).strip().lower() != "phone":
            continue
        target_id = safe_id(str(raw.get("id") or raw.get("phone") or ""))
        patch = patch_by_id.get(target_id)
        if not patch:
            continue

        if patch.get("phone") is not None:
            phone = normalize_phone_digits(str(patch.get("phone") or ""))
            if phone:
                raw["phone"] = phone

        send_block = raw.get("send")
        if not isinstance(send_block, dict):
            send_block = {}
            raw["send"] = send_block

        if patch.get("message") is not None:
            send_block["message"] = str(patch.get("message") or "")

        if patch.get("enabled") is not None:
            enabled = bool(patch.get("enabled"))
            raw["enabled"] = enabled
            send_block["enabled"] = enabled

        if patch.get("name") is not None:
            raw["name"] = str(patch.get("name") or "").strip()

        changed += 1

    targets_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return {
        "ok": True,
        "updated_count": changed,
        "targets_path": str(targets_path),
    }


def _apply_target_type_selection(
    targets_path: Path,
    selected_ids: list[str],
    *,
    target_type: str,
    default_message: str | None = None,
) -> dict[str, Any]:
    if not targets_path.exists():
        raise FileNotFoundError(f"Arquivo de alvos não encontrado: {targets_path}")

    data = json.loads(targets_path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("Formato de alvos inválido.")

    selected = {safe_id(item) for item in selected_ids if item}
    enabled_count = 0
    normalized_type = target_type.strip().lower()

    for raw in data.get("targets") or []:
        if str(raw.get("type", "")).strip().lower() != normalized_type:
            continue
        target_id = safe_id(str(raw.get("id") or raw.get("phone") or raw.get("name") or ""))
        is_selected = target_id in selected
        raw["enabled"] = is_selected
        send_block = raw.get("send")
        if not isinstance(send_block, dict):
            send_block = {}
            raw["send"] = send_block
        send_block["enabled"] = is_selected
        if is_selected and default_message:
            send_block["message"] = default_message
        if is_selected:
            enabled_count += 1

    targets_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return {
        "ok": True,
        "selected_count": len(selected),
        "enabled_count": enabled_count,
        "targets_path": str(targets_path),
    }


def apply_group_selection(
    targets_path: Path,
    selected_ids: list[str],
    *,
    default_message: str | None = None,
) -> dict[str, Any]:
    """Persiste quais grupos estão habilitados para envio no JSON de targets."""
    return _apply_target_type_selection(
        targets_path,
        selected_ids,
        target_type="group",
        default_message=default_message,
    )


def apply_phone_selection(
    targets_path: Path,
    selected_ids: list[str],
    *,
    default_message: str | None = None,
) -> dict[str, Any]:
    """Persiste quais números estão habilitados para envio no JSON de targets."""
    return _apply_target_type_selection(
        targets_path,
        selected_ids,
        target_type="phone",
        default_message=default_message,
    )


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
            await shutdown_playwright_stack(
                pages=[page],
                context=context,
                playwright=playwright,
            )


async def execute_list_groups_job(
    env_file: Path,
    *,
    output_path: Path,
    targets_output_path: Path | None = None,
    merge_targets_path: Path | None = None,
    resolve_names: list[str] | None = None,
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
        if merge_targets_path is not None:
            merge_targets_path = resolve_project_path(merge_targets_path)

        playwright, context, page = await open_whatsapp(bootstrap.config)
        try:
            await wait_for_whatsapp_ready(page, bootstrap.config.ready_timeout)
            resolve_names_list: list[str] = []
            seen_resolve: set[str] = set()
            for name in resolve_names or []:
                cleaned = str(name).strip()
                if not cleaned:
                    continue
                key = cleaned.casefold()
                if key in seen_resolve:
                    continue
                seen_resolve.add(key)
                resolve_names_list.append(cleaned)

            for candidate_path in (merge_targets_path, targets_output_path):
                if candidate_path is None:
                    continue
                for name in list_group_names_from_targets_file(candidate_path):
                    key = name.casefold()
                    if key in seen_resolve:
                        continue
                    seen_resolve.add(key)
                    resolve_names_list.append(name)

            result = await extract_whatsapp_groups(page, resolve_names=resolve_names_list)

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

            merge_outcome: dict[str, Any] | None = None
            if merge_targets_path is not None:
                merge_outcome = merge_groups_into_targets(merge_targets_path, groups)

            response = {
                "ok": bool(result.get("ok")) or len(groups) > 0,
                "total_groups": len(groups),
                "groups": groups,
                "generated_at": result.get("generated_at"),
                "diagnostics": result.get("diagnostics"),
                "inventory_path": str(output_path),
                "targets_path": targets_path,
            }
            if merge_outcome:
                response.update(merge_outcome)
            return response
        finally:
            await shutdown_playwright_stack(
                pages=[page],
                context=context,
                playwright=playwright,
            )


async def execute_sync_contacts_job(
    env_file: Path,
    *,
    output_path: Path,
    targets_path: Path,
) -> dict[str, Any]:
    """Lista contatos do WhatsApp Web e mescla números em targets.json."""
    if _job_lock.locked():
        return {
            "ok": False,
            "error": "Já existe um envio ou automação em andamento. Tente novamente em instantes.",
            "busy": True,
            "total_contacts": 0,
            "contacts": [],
        }

    async with _job_lock:
        bootstrap = load_env_before_browser(env_file)
        bootstrap.config.export_dir.mkdir(parents=True, exist_ok=True)

        output_path = resolve_project_path(output_path)
        targets_path = resolve_project_path(targets_path)

        playwright, context, page = await open_whatsapp(bootstrap.config)
        try:
            await wait_for_whatsapp_ready(page, bootstrap.config.ready_timeout)
            result = await extract_whatsapp_contacts(page)

            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_text(
                json.dumps(result, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )

            contacts = result.get("contacts") or []
            merge_outcome = merge_contacts_into_targets(targets_path, contacts)

            return {
                "ok": bool(result.get("ok")) or len(contacts) > 0,
                "total_contacts": len(contacts),
                "contacts": contacts,
                "generated_at": result.get("generated_at"),
                "diagnostics": result.get("diagnostics"),
                "inventory_path": str(output_path),
                "targets_path": str(targets_path),
                **merge_outcome,
            }
        finally:
            await shutdown_playwright_stack(
                pages=[page],
                context=context,
                playwright=playwright,
            )
