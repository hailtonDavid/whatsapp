"""Orquestração da automação WhatsApp Web (env → browser → envio/grupos)."""

from __future__ import annotations

import asyncio
import json
import os
import platform
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from playwright.async_api import BrowserContext, Page, Playwright

from browser_service import initialize_browser, wait_for_login_element
from playwright_lifecycle import drain_event_loop_subprocesses, shutdown_playwright_stack
from session_state import SessionState, resolve_session_state, wait_for_stable_session_state
from conversation_store import ConversationStore, conversation_key_for, get_conversation_store
from semantic_store import index_messages_for_search
from whatsapp_auto_downloader import (
    AppConfig,
    Target,
    TargetsConfig,
    build_groups_targets_json,
    collect_messages_for_target,
    extract_whatsapp_contacts,
    extract_whatsapp_groups,
    find_playwright_chromium_processes_windows,
    find_profile_processes,
    kill_processes,
    kill_processes_windows,
    load_app_config,
    load_targets_config,
    merge_contacts_into_targets,
    merge_groups_into_targets,
    normalize_phone_digits,
    open_chat_by_name,
    open_target,
    open_whatsapp,
    remove_lock_files,
    resolve_project_path,
    run_send_once,
    safe_id,
    SCROLL_CHAT_BOTTOM_JS,
)

_job_lock = asyncio.Lock()
_ensure_auth_lock = asyncio.Lock()
_held_session: AutomationSession | None = None

_AUTH_PROBE_TIMEOUT_MS = 10_000
_JOB_SESSION_TIMEOUT_MS = 35_000
_MAX_UPLOAD_BYTES = 64 * 1024 * 1024


def automation_job_timeout(config: AppConfig, *, heavy: bool = False) -> float:
    """Tempo máximo de jobs Playwright (segundos). Grupos/contatos podem levar vários minutos."""
    base = float(config.ready_timeout)
    if heavy:
        return max(900.0, base * 2 + 420)
    return max(420.0, base + 180)


_MAX_RESOLVE_GROUP_NAMES = 25


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


async def stop_automation(
    env_file: Path | None = None,
    *,
    unlock_profile: bool = True,
) -> dict[str, Any]:
    """Encerra sessão Playwright ativa e libera o perfil do Edge."""
    global _held_session
    stopped = False
    if _held_session is not None:
        session = _held_session
        _held_session = None
        await shutdown_automation_session(session)
        stopped = True

    await drain_event_loop_subprocesses()
    if unlock_profile:
        profile_dir = _profile_dir_from_env(env_file)
        if profile_dir is not None:
            unlock_profile_for_launch(profile_dir)
            await asyncio.sleep(0.75)

    return {
        "ok": True,
        "session_active": False,
        "stopped": stopped,
        "profile_unlocked": unlock_profile,
    }


def _profile_dir_from_env(env_file: Path | None) -> Path | None:
    if env_file is None:
        return None
    try:
        return load_env_before_browser(env_file).config.profile_dir
    except RuntimeError:
        return None


def unlock_profile_for_launch(profile_dir: Path) -> int:
    """Encerra processos e remove locks órfãos do perfil persistente."""
    killed = 0
    processes = find_profile_processes(profile_dir)
    if processes:
        killed += kill_processes(processes)
    if platform.system().lower().startswith("win"):
        pw = find_playwright_chromium_processes_windows()
        if pw:
            killed += kill_processes_windows(pw)
    remove_lock_files(profile_dir)
    return killed


async def release_browser_resources(
    env_file: Path | None = None,
    *,
    unlock_profile: bool = False,
) -> None:
    """Fecha sessão ativa e opcionalmente desbloqueia o perfil."""
    global _held_session
    if _held_session is not None:
        await shutdown_automation_session(_held_session)
        _held_session = None
    await drain_event_loop_subprocesses()
    if unlock_profile:
        profile_dir = _profile_dir_from_env(env_file)
        if profile_dir is not None:
            unlock_profile_for_launch(profile_dir)
            await asyncio.sleep(0.75)


async def open_whatsapp_resilient(
    config: AppConfig,
    env_file: Path,
) -> tuple[Playwright, BrowserContext, Page]:
    """Abre WhatsApp Web; desbloqueia perfil e tenta de novo se o Edge estiver preso."""
    last_error: RuntimeError | None = None
    for attempt in range(2):
        try:
            return await open_whatsapp(config)
        except RuntimeError as exc:
            last_error = exc
            msg = str(exc).lower()
            if attempt == 0 and "perfil persistente" in msg:
                await release_browser_resources(env_file, unlock_profile=True)
                continue
            raise
    if last_error is not None:
        raise last_error
    raise RuntimeError("Falha ao abrir WhatsApp Web.")


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
    session_state: SessionState | None = None


@dataclass
class WhatsAppOperation:
    bootstrap: AutomationBootstrap
    playwright: Playwright
    context: BrowserContext
    page: Page
    session_state: SessionState
    keep_browser_open: bool = False


def is_whatsapp_authorized(state: SessionState | None) -> bool:
    return state == "logged_in"


def auth_status_fields(
    state: SessionState | None,
    *,
    session_active: bool,
    headless: bool | None = None,
) -> dict[str, Any]:
    authorized = is_whatsapp_authorized(state) if state is not None else None
    fields: dict[str, Any] = {
        "session_state": state,
        "whatsapp_authorized": authorized,
        "session_active": session_active,
    }
    if headless is not None:
        fields["headless"] = headless
    if authorized is False:
        fields["requires_qr"] = state == "login_qr"
    return fields


async def _close_held_session_if_authorized_headless(
    env_file: Path,
    *,
    state: SessionState | None = None,
) -> SessionState | None:
    """Fecha o Edge quando autorizado e WA_HEADLESS=true — só mantém aberto para QR."""
    global _held_session
    if _held_session is None:
        return state

    if state is None:
        state = await wait_for_stable_session_state(
            _held_session.page,
            timeout_ms=_AUTH_PROBE_TIMEOUT_MS,
        )
        _held_session.session_state = state

    if not is_whatsapp_authorized(state):
        return state

    bootstrap = load_env_before_browser(env_file)
    if bootstrap.config.headless and not _held_session.bootstrap.config.headless:
        await shutdown_automation_session(_held_session)
        _held_session = None
        await drain_event_loop_subprocesses()
    return state


async def probe_held_session_auth(
    env_file: Path | None = None,
    *,
    timeout_ms: int = 5_000,
) -> dict[str, Any]:
    """Lê estado de autorização da sessão Playwright mantida aberta."""
    if _held_session is None:
        return auth_status_fields(None, session_active=False)

    state = await wait_for_stable_session_state(_held_session.page, timeout_ms=timeout_ms)
    _held_session.session_state = state

    if env_file is not None:
        state = await _close_held_session_if_authorized_headless(env_file, state=state)
        if _held_session is None:
            bootstrap = load_env_before_browser(env_file)
            return auth_status_fields(
                state,
                session_active=False,
                headless=bootstrap.config.headless,
            )

    return auth_status_fields(
        state,
        session_active=True,
        headless=_held_session.bootstrap.config.headless,
    )


async def _open_visible_held_session(env_file: Path) -> AutomationSession:
    """Abre sessão visível — apenas para escanear QR Code."""
    global _held_session
    if _held_session is not None:
        await shutdown_automation_session(_held_session)
        _held_session = None
        await drain_event_loop_subprocesses()
    session = await launch_automation(env_file, headless=False, force=False)
    try:
        await session.page.bring_to_front()
    except Exception:
        pass
    return session


async def _probe_auth_headless(env_file: Path) -> tuple[SessionState, bool]:
    """Verifica autorização em headless; retorna (estado, abriu_janela_visível)."""
    bootstrap = load_env_before_browser(env_file)
    if not bootstrap.config.headless:
        session = await launch_automation(env_file, headless=False, force=False)
        state = session.session_state or await wait_for_stable_session_state(
            session.page,
            timeout_ms=_AUTH_PROBE_TIMEOUT_MS,
        )
        session.session_state = state
        return state, not is_whatsapp_authorized(state)

    playwright, context, page = await open_whatsapp_resilient(bootstrap.config, env_file)
    transferred = False
    try:
        state = await wait_for_stable_session_state(page, timeout_ms=_AUTH_PROBE_TIMEOUT_MS)
        if is_whatsapp_authorized(state):
            return state, False
        if state == "login_qr":
            transferred = True
            await shutdown_playwright_stack(pages=[page], context=context, playwright=playwright)
            await _open_visible_held_session(env_file)
            qr_state = _held_session.session_state if _held_session else state
            return qr_state or state, True
        return state, False
    finally:
        if not transferred:
            await shutdown_playwright_stack(pages=[page], context=context, playwright=playwright)


async def ensure_whatsapp_authorized(env_file: Path) -> dict[str, Any]:
    """Verifica autorização; abre navegador visível automaticamente se precisar de QR."""
    if _job_lock.locked():
        return {
            "ok": False,
            "busy": True,
            "error": "Outra operação em andamento. Aguarde e tente novamente.",
            **auth_status_fields(None, session_active=automation_is_running()),
        }

    if _ensure_auth_lock.locked():
        return {
            "ok": False,
            "busy": True,
            "error": "Verificação de autorização já em andamento.",
            **auth_status_fields(None, session_active=automation_is_running()),
        }

    async with _ensure_auth_lock:
        return await _ensure_whatsapp_authorized_unlocked(env_file)


async def _ensure_whatsapp_authorized_unlocked(env_file: Path) -> dict[str, Any]:
    global _held_session

    if _held_session is not None:
        state = await _probe_held_session_state()
        if _held_session is not None:
            _held_session.session_state = state
            if state == "login_qr" and _held_session.bootstrap.config.headless:
                await _open_visible_held_session(env_file)
                state = _held_session.session_state or "login_qr"
            else:
                state = await _close_held_session_if_authorized_headless(env_file, state=state) or state

            if _held_session is None:
                bootstrap = load_env_before_browser(env_file)
                payload = auth_status_fields(
                    state,
                    session_active=False,
                    headless=bootstrap.config.headless,
                )
                payload["ok"] = is_whatsapp_authorized(state)
                return payload

            payload = auth_status_fields(
                state,
                session_active=True,
                headless=_held_session.bootstrap.config.headless,
            )
            payload["ok"] = is_whatsapp_authorized(state)
            if not payload["ok"]:
                payload["message"] = "Escaneie o QR Code na janela do Edge para autorizar o WhatsApp Web."
            return payload

    # Sem sessão: headless primeiro — visível só se precisar de QR.
    state, _opened_visible = await _probe_auth_headless(env_file)

    if is_whatsapp_authorized(state):
        bootstrap = load_env_before_browser(env_file)
        return {
            "ok": True,
            **auth_status_fields(
                state,
                session_active=automation_is_running(),
                headless=bootstrap.config.headless,
            ),
        }

    if _held_session is not None:
        payload = auth_status_fields(state, session_active=True, headless=False)
        payload["ok"] = False
        payload["message"] = "Escaneie o QR Code na janela do Edge para autorizar o WhatsApp Web."
        return payload

    payload = auth_status_fields(state, session_active=False, headless=True)
    payload["ok"] = False
    payload["message"] = (
        "WhatsApp não autorizado. Clique em «Abrir visível (QR Code)» no painel para escanear."
    )
    return payload


def _auth_failure_message(state: SessionState, *, headless: bool) -> str:
    if state == "login_qr":
        return (
            "WhatsApp não autorizado. Clique em «Abrir visível (QR Code)» no painel para escanear."
            if headless
            else "WhatsApp não autorizado. Escaneie o QR Code na janela aberta do Edge."
        )
    if state == "unknown":
        return (
            "Não foi possível confirmar a sessão do WhatsApp Web. "
            "Aguarde a sincronização, clique em «Abrir visível (QR Code)» ou tente novamente."
        )
    return "WhatsApp não autorizado."


async def _clear_held_session_if_stale() -> None:
    global _held_session
    if _held_session is None:
        return
    try:
        if _held_session.page.is_closed():
            raise RuntimeError("held page closed")
    except Exception:
        await shutdown_automation_session(_held_session)
        _held_session = None
        await drain_event_loop_subprocesses()


async def _probe_held_session_state() -> SessionState:
    """Lê estado da sessão ativa; descarta referência se o navegador foi fechado."""
    global _held_session
    if _held_session is None:
        return "unknown"
    try:
        if _held_session.page.is_closed():
            raise RuntimeError("held page closed")
        return await wait_for_stable_session_state(
            _held_session.page,
            timeout_ms=_AUTH_PROBE_TIMEOUT_MS,
        )
    except Exception:
        await shutdown_automation_session(_held_session)
        _held_session = None
        await drain_event_loop_subprocesses()
        return "unknown"


async def connect_whatsapp_for_operation(
    env_file: Path,
) -> WhatsAppOperation | dict[str, Any]:
    """Abre WhatsApp para um job; reutiliza sessão ativa para evitar conflito de perfil."""
    global _held_session

    await _clear_held_session_if_stale()

    if _held_session is not None:
        state = await _probe_held_session_state()
        if _held_session is not None:
            _held_session.session_state = state
            if not is_whatsapp_authorized(state):
                if not _held_session.bootstrap.config.headless:
                    try:
                        await _held_session.page.bring_to_front()
                    except Exception:
                        pass
                return {
                    "ok": False,
                    **auth_status_fields(
                        state,
                        session_active=True,
                        headless=_held_session.bootstrap.config.headless,
                    ),
                    "message": _auth_failure_message(
                        state,
                        headless=_held_session.bootstrap.config.headless,
                    ),
                }

            bootstrap = load_env_before_browser(env_file)
            if bootstrap.config.headless and not _held_session.bootstrap.config.headless:
                await shutdown_automation_session(_held_session)
                _held_session = None
                await drain_event_loop_subprocesses()
            else:
                return WhatsAppOperation(
                    bootstrap=_held_session.bootstrap,
                    playwright=_held_session.playwright,
                    context=_held_session.context,
                    page=_held_session.page,
                    session_state=state,
                    keep_browser_open=not bootstrap.config.headless,
                )

    bootstrap = load_env_before_browser(env_file)
    await release_browser_resources(env_file, unlock_profile=True)
    playwright, context, page = await open_whatsapp_resilient(bootstrap.config, env_file)
    state = await resolve_session_state(
        page,
        timeout_ms=_JOB_SESSION_TIMEOUT_MS,
        ready_timeout_seconds=bootstrap.config.ready_timeout,
    )

    if not is_whatsapp_authorized(state):
        await shutdown_playwright_stack(pages=[page], context=context, playwright=playwright)
        return {
            "ok": False,
            **auth_status_fields(
                state,
                session_active=automation_is_running(),
                headless=bootstrap.config.headless,
            ),
            "message": _auth_failure_message(state, headless=bootstrap.config.headless),
        }

    return WhatsAppOperation(
        bootstrap=bootstrap,
        playwright=playwright,
        context=context,
        page=page,
        session_state=state,
    )


async def release_whatsapp_operation(
    operation: WhatsAppOperation,
    env_file: Path | None = None,
) -> None:
    if operation.keep_browser_open:
        if env_file is not None:
            await _close_held_session_if_authorized_headless(env_file)
        return
    await shutdown_playwright_stack(
        pages=[operation.page],
        context=operation.context,
        playwright=operation.playwright,
    )


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
    try:
        state = await wait_for_stable_session_state(page, timeout_ms=_AUTH_PROBE_TIMEOUT_MS)
    except Exception as exc:
        await shutdown_playwright_stack(pages=[page], context=context, playwright=playwright)
        msg = str(exc).lower()
        if "closed" in msg or "target" in msg:
            raise RuntimeError(
                "O navegador fechou antes de carregar o WhatsApp Web. "
                "Feche janelas antigas do Edge e use «Abrir visível (QR Code)»."
            ) from exc
        raise RuntimeError(f"Falha ao iniciar WhatsApp Web: {exc}") from exc

    return AutomationSession(
        bootstrap=bootstrap,
        playwright=playwright,
        context=context,
        page=page,
        session_state=state,
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


def apply_group_updates(
    targets_path: Path,
    updates: list[dict[str, Any]],
) -> dict[str, Any]:
    """Atualiza mensagem e/ou enabled de alvos type=group."""
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
        if str(raw.get("type", "")).strip().lower() != "group":
            continue
        target_id = safe_id(str(raw.get("id") or ""))
        patch = patch_by_id.get(target_id)
        if not patch:
            continue

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


def uploads_dir_for_config(config: AppConfig) -> Path:
    path = resolve_project_path(config.export_dir / "uploads")
    path.mkdir(parents=True, exist_ok=True)
    return path


def sanitize_upload_filename(name: str) -> str:
    base = Path(name or "anexo").name
    cleaned = safe_id(Path(base).stem) or "anexo"
    suffix = Path(base).suffix.lower()[:16]
    return f"{cleaned}{suffix}"


def resolve_allowed_attachment_path(config: AppConfig, attachment: str) -> Path:
    if not str(attachment or "").strip():
        raise ValueError("Caminho do anexo vazio.")

    uploads_root = uploads_dir_for_config(config).resolve()
    raw = Path(str(attachment).strip())
    candidates = [
        resolve_project_path(raw),
        uploads_root / raw.name,
    ]
    if not raw.is_absolute():
        candidates.append(uploads_root / raw)
        candidates.append(resolve_project_path(config.export_dir.parent / raw))

    path: Path | None = None
    for candidate in candidates:
        try:
            resolved = candidate.resolve()
        except OSError:
            continue
        if resolved.is_file():
            path = resolved
            break

    if path is None:
        raise FileNotFoundError(f"Anexo não encontrado: {attachment}")

    try:
        path.relative_to(uploads_root)
    except ValueError as exc:
        raise ValueError("Anexo deve estar em exports/uploads/.") from exc

    if path.stat().st_size > _MAX_UPLOAD_BYTES:
        raise ValueError("Anexo excede o tamanho máximo permitido (64 MB).")
    return path


def public_path_for_saved_file(config: AppConfig, target: Path) -> str:
    resolved = target.resolve()
    roots = [
        resolve_project_path(Path(".")).resolve(),
        resolve_project_path(config.export_dir).resolve().parent,
    ]
    for root in roots:
        try:
            return resolved.relative_to(root).as_posix()
        except ValueError:
            continue
    return resolved.as_posix()


def save_uploaded_attachment(config: AppConfig, filename: str, data: bytes) -> dict[str, Any]:
    if len(data) > _MAX_UPLOAD_BYTES:
        raise ValueError("Arquivo excede o tamanho máximo permitido (64 MB).")

    uploads_dir = uploads_dir_for_config(config)
    safe_name = sanitize_upload_filename(filename)
    target = uploads_dir / safe_name
    if target.exists():
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        target = uploads_dir / f"{target.stem}_{stamp}{target.suffix}"
    target.write_bytes(data)
    relative = public_path_for_saved_file(config, target)
    return {
        "ok": True,
        "path": relative,
        "filename": target.name,
        "size_bytes": len(data),
    }


async def execute_send_job(
    env_file: Path,
    targets_path: Path,
    *,
    message: str | None = None,
    target_ids: list[str] | None = None,
    allow_all: bool = False,
    confirm: bool = False,
    attachment: str | None = None,
) -> dict[str, Any]:
    """Executa envio único via Playwright (simulação ou envio real)."""
    has_message = message is not None and str(message).strip() != ""
    has_attachment = bool(str(attachment or "").strip())
    if (has_message or has_attachment) and not target_ids and not allow_all:
        return {
            "ok": False,
            "error": "Informe target_ids ou allow_all=true ao usar message/anexo.",
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
        attachment_path: str | None = None
        if has_attachment:
            try:
                attachment_path = str(
                    resolve_allowed_attachment_path(bootstrap.config, str(attachment))
                )
            except (FileNotFoundError, ValueError) as exc:
                return {"ok": False, "error": str(exc), "results": []}

        if dry_run:
            results = await run_send_once(
                page=None,
                app_config=bootstrap.config,
                targets_config=targets_config,
                message_override=message,
                target_ids=target_ids,
                dry_run=True,
                attachment_path=attachment_path,
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

        connection = await connect_whatsapp_for_operation(env_file)
        if isinstance(connection, dict):
            return {**connection, "results": []}

        try:
            results = await run_send_once(
                page=connection.page,
                app_config=connection.bootstrap.config,
                targets_config=targets_config,
                message_override=message,
                target_ids=target_ids,
                dry_run=False,
                attachment_path=attachment_path,
            )
            await connection.page.wait_for_timeout(5000)
            total_ok = sum(1 for item in results if item.get("ok"))
            return {
                "ok": total_ok > 0,
                "dry_run": False,
                "confirmed": True,
                "total": len(results),
                "successes": total_ok,
                "results": results,
                "summary_path": str(connection.bootstrap.config.export_dir / "send" / "last_send.json"),
            }
        finally:
            await release_whatsapp_operation(connection, env_file)


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

        connection = await connect_whatsapp_for_operation(env_file)
        if isinstance(connection, dict):
            return {**connection, "total_groups": 0, "groups": []}

        page = connection.page
        bootstrap = connection.bootstrap
        try:
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
                if len(resolve_names_list) >= _MAX_RESOLVE_GROUP_NAMES:
                    break

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
            await release_whatsapp_operation(connection, env_file)


def build_phone_target(phone: str, *, name: str | None = None, target_id: str | None = None) -> Target:
    digits = normalize_phone_digits(phone)
    if not digits:
        raise ValueError(f"Telefone inválido: {phone}")
    resolved_id = safe_id(target_id or f"numero_{digits}")
    return Target(
        id=resolved_id,
        type="phone",
        phone=digits,
        name=name,
        enabled=True,
    )


def resolve_read_targets(
    targets_path: Path,
    *,
    target_ids: list[str] | None = None,
    phones: list[str] | None = None,
) -> list[Target]:
    """Resolve alvos para leitura de conversa a partir de IDs ou números."""
    selected_ids = {safe_id(item) for item in (target_ids or []) if item}
    targets_by_id: dict[str, Target] = {}

    if targets_path.is_file():
        for target in load_targets_config(targets_path).targets:
            if target.type == "phone":
                targets_by_id[target.id] = target

    resolved: list[Target] = []
    seen_keys: set[str] = set()

    for raw_id in selected_ids:
        target = targets_by_id.get(raw_id)
        if target is None:
            phone_guess = raw_id.replace("numero_", "")
            if normalize_phone_digits(phone_guess):
                target = build_phone_target(phone_guess, target_id=raw_id)
            else:
                continue
        key = conversation_key_for(phone=target.phone, target_id=target.id)
        if key in seen_keys:
            continue
        seen_keys.add(key)
        resolved.append(target)

    for raw_phone in phones or []:
        try:
            target = build_phone_target(str(raw_phone))
        except ValueError:
            continue
        existing = targets_by_id.get(target.id)
        if existing:
            target = existing
        key = conversation_key_for(phone=target.phone, target_id=target.id)
        if key in seen_keys:
            continue
        seen_keys.add(key)
        resolved.append(target)

    return resolved


def resolve_single_read_target(
    targets_path: Path,
    *,
    target_ids: list[str] | None = None,
    phones: list[str] | None = None,
    phone: str | None = None,
    target_id: str | None = None,
) -> Target:
    """Resolve um único alvo para leitura/preview de conversa."""
    if phone and str(phone).strip():
        targets = resolve_read_targets(targets_path, phones=[str(phone).strip()])
    elif target_id and str(target_id).strip():
        targets = resolve_read_targets(targets_path, target_ids=[str(target_id).strip()])
    else:
        cleaned_phones = [str(item) for item in (phones or []) if item]
        cleaned_ids = [str(item) for item in (target_ids or []) if item]

        if cleaned_phones:
            targets = resolve_read_targets(targets_path, phones=[cleaned_phones[0]])
        elif cleaned_ids:
            targets = resolve_read_targets(targets_path, target_ids=[cleaned_ids[0]])
        else:
            targets = []

    if not targets:
        raise ValueError("Selecione um número válido para ler a conversa.")
    return targets[0]


async def _read_messages_from_whatsapp(
    page: Page,
    target: Target,
    *,
    scrolls: int,
    delay: float,
) -> tuple[bool, str | None, list[dict[str, Any]]]:
    opened, open_error = await open_target(page, target)
    if not opened:
        return False, open_error or "Não abriu a conversa.", []

    await page.wait_for_timeout(3500)
    await page.evaluate(SCROLL_CHAT_BOTTOM_JS)
    await page.wait_for_timeout(1500)

    messages = await collect_messages_for_target(
        page=page,
        target=target,
        scrolls=max(1, scrolls),
        delay=delay,
    )

    if not messages and target.name:
        opened_by_name, name_error = await open_chat_by_name(page, target.name)
        if opened_by_name:
            await page.wait_for_timeout(3500)
            await page.evaluate(SCROLL_CHAT_BOTTOM_JS)
            await page.wait_for_timeout(1500)
            messages = await collect_messages_for_target(
                page=page,
                target=target,
                scrolls=max(1, scrolls),
                delay=delay,
            )
        elif not open_error:
            open_error = name_error

    if not messages:
        await page.wait_for_timeout(2500)
        await page.evaluate(SCROLL_CHAT_BOTTOM_JS)
        messages = await collect_messages_for_target(
            page=page,
            target=target,
            scrolls=max(1, scrolls),
            delay=delay,
        )

    return True, None, messages


def _annotate_messages_with_saved_state(
    messages: list[dict[str, Any]],
    saved_hashes: set[str],
) -> list[dict[str, Any]]:
    annotated: list[dict[str, Any]] = []
    for msg in messages:
        msg_hash = str(msg.get("hash") or "")
        annotated.append(
            {
                **msg,
                "saved_in_db": msg_hash in saved_hashes if msg_hash else False,
            }
        )
    return annotated


async def execute_preview_conversation_job(
    env_file: Path,
    *,
    targets_path: Path,
    target_ids: list[str] | None = None,
    phones: list[str] | None = None,
    scrolls: int | None = None,
    store: ConversationStore | None = None,
) -> dict[str, Any]:
    """Lê mensagens de um número no WhatsApp Web sem gravar no MongoDB."""
    conversation_store = store or get_conversation_store()
    resolved_targets_path = resolve_project_path(targets_path)

    try:
        target = resolve_single_read_target(
            resolved_targets_path,
            target_ids=target_ids,
            phones=phones,
            phone=(phones[0] if phones and len(phones) == 1 else None),
            target_id=(target_ids[0] if target_ids and len(target_ids) == 1 else None),
        )
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}

    if _job_lock.locked():
        return {
            "ok": False,
            "error": "Já existe um job em andamento. Tente novamente em instantes.",
            "busy": True,
        }

    async with _job_lock:
        if resolved_targets_path.is_file():
            targets_config = load_targets_config(resolved_targets_path)
        else:
            targets_config = TargetsConfig()
        effective_scrolls = scrolls if scrolls is not None else targets_config.scrolls_per_target
        delay = targets_config.delay_between_scrolls

        connection = await connect_whatsapp_for_operation(env_file)
        if isinstance(connection, dict):
            return connection

        page = connection.page
        try:
            ok, error, messages = await _read_messages_from_whatsapp(
                page,
                target,
                scrolls=effective_scrolls,
                delay=delay,
            )
            if not ok:
                return {
                    "ok": False,
                    "error": error,
                    "target_id": target.id,
                    "phone": target.phone,
                }

            key = conversation_key_for(phone=target.phone, target_id=target.id)
            saved_hashes: set[str] = set()
            if conversation_store.enabled:
                saved_hashes = conversation_store.get_saved_hashes(key)

            annotated = _annotate_messages_with_saved_state(messages, saved_hashes)
            already_saved = sum(1 for msg in annotated if msg.get("saved_in_db"))
            outcome: dict[str, Any] = {
                "ok": True,
                "preview": True,
                "target_id": target.id,
                "phone": target.phone,
                "target_name": target.name,
                "conversation_key": key,
                "captured_messages": len(annotated),
                "already_saved_count": already_saved,
                "mongodb_configured": conversation_store.enabled,
                "messages": annotated,
            }
            if not annotated:
                outcome["warning"] = (
                    "A conversa foi aberta, mas nenhuma mensagem visível foi capturada. "
                    "Confirme se o WhatsApp está conectado e se há histórico neste chat."
                )
            return outcome
        finally:
            await release_whatsapp_operation(connection, env_file)


def save_selected_conversation_messages(
    *,
    phone: str | None = None,
    target_id: str | None = None,
    target_name: str | None = None,
    target_type: str = "phone",
    messages: list[dict[str, Any]],
    store: ConversationStore | None = None,
) -> dict[str, Any]:
    """Grava no MongoDB apenas as mensagens escolhidas pelo usuário."""
    conversation_store = store or get_conversation_store()
    if not conversation_store.enabled:
        return {"ok": False, "error": "MongoDB não configurado. Defina MONGODB_URI no .env."}

    ping = conversation_store.ping()
    if not ping.get("ok"):
        return {"ok": False, "error": ping.get("error")}

    if not messages:
        return {"ok": False, "error": "Nenhuma mensagem selecionada para salvar."}

    key = conversation_key_for(phone=phone, target_id=target_id)
    resolved_target_id = safe_id(target_id or f"numero_{normalize_phone_digits(phone or '')}")
    stats = conversation_store.save_conversation_messages(
        conversation_key=key,
        target_id=resolved_target_id,
        target_type=target_type,
        phone=normalize_phone_digits(phone or "") or None,
        target_name=target_name,
        messages=messages,
    )
    semantic_stats = index_messages_for_search(
        phone=phone,
        target_id=target_id,
        target_name=target_name,
        target_type=target_type,
        messages=messages,
    )
    return {
        "ok": True,
        "conversation_key": key,
        "saved_count": len(messages),
        **stats,
        "mongodb": ping,
        "semantic": semantic_stats,
    }


async def execute_read_conversations_job(
    env_file: Path,
    *,
    targets_path: Path,
    target_ids: list[str] | None = None,
    phones: list[str] | None = None,
    scrolls: int | None = None,
    store: ConversationStore | None = None,
) -> dict[str, Any]:
    """Lê conversas do WhatsApp Web e grava no MongoDB."""
    conversation_store = store or get_conversation_store()
    if not conversation_store.enabled:
        return {
            "ok": False,
            "error": "MongoDB não configurado. Defina MONGODB_URI no .env.",
            "results": [],
        }

    ping = conversation_store.ping()
    if not ping.get("ok"):
        return {"ok": False, "error": ping.get("error"), "results": []}

    targets = resolve_read_targets(
        resolve_project_path(targets_path),
        target_ids=target_ids,
        phones=phones,
    )
    if not targets:
        return {
            "ok": False,
            "error": "Nenhum alvo válido. Informe target_ids ou phones.",
            "results": [],
        }

    if _job_lock.locked():
        return {
            "ok": False,
            "error": "Já existe um job em andamento. Tente novamente em instantes.",
            "busy": True,
            "results": [],
        }

    async with _job_lock:
        bootstrap = load_env_before_browser(env_file)
        resolved_targets_path = resolve_project_path(targets_path)
        if resolved_targets_path.is_file():
            targets_config = load_targets_config(resolved_targets_path)
        else:
            targets_config = TargetsConfig()
        effective_scrolls = scrolls if scrolls is not None else targets_config.scrolls_per_target
        delay = targets_config.delay_between_scrolls

        connection = await connect_whatsapp_for_operation(env_file)
        if isinstance(connection, dict):
            return {**connection, "results": []}

        page = connection.page
        results: list[dict[str, Any]] = []

        try:
            for target in targets:
                item: dict[str, Any] = {
                    "target_id": target.id,
                    "phone": target.phone,
                    "target_name": target.name,
                }
                try:
                    ok, open_error, messages = await _read_messages_from_whatsapp(
                        page,
                        target,
                        scrolls=effective_scrolls,
                        delay=delay,
                    )
                    if not ok:
                        item.update({"ok": False, "error": open_error or "Não abriu a conversa."})
                        results.append(item)
                        continue

                    key = conversation_key_for(phone=target.phone, target_id=target.id)
                    save_stats = conversation_store.save_conversation_messages(
                        conversation_key=key,
                        target_id=target.id,
                        target_type=target.type,
                        phone=target.phone,
                        target_name=target.name,
                        messages=messages,
                    )
                    item.update(
                        {
                            "ok": True,
                            "conversation_key": key,
                            "captured_messages": len(messages),
                            **save_stats,
                        }
                    )
                except Exception as exc:
                    item.update({"ok": False, "error": f"{type(exc).__name__}: {exc}"})
                results.append(item)
                await page.wait_for_timeout(int(targets_config.delay_between_targets * 1000))
        finally:
            await release_whatsapp_operation(connection, env_file)

        successes = sum(1 for row in results if row.get("ok"))
        return {
            "ok": successes > 0,
            "total": len(results),
            "successes": successes,
            "results": results,
            "mongodb": ping,
        }


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

        connection = await connect_whatsapp_for_operation(env_file)
        if isinstance(connection, dict):
            return {**connection, "total_contacts": 0, "contacts": []}

        page = connection.page
        bootstrap = connection.bootstrap
        try:
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
            await release_whatsapp_operation(connection, env_file)
