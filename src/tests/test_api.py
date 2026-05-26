"""RF05 — endpoints de controle Flask e integração com o motor de automação."""

from __future__ import annotations

from collections.abc import Generator
from pathlib import Path

import pytest

from automation_service import (
    automation_is_running,
    reset_held_automation_session_for_tests,
    start_automation,
)
from wa_selectors import WHATSAPP_LOGIN_SELECTOR
from whatsapp_auto_downloader import WA_URL

pytestmark = [pytest.mark.integration, pytest.mark.rf05]

RF05_CONTROL_GET_ROUTES = (
    ("/api/automation/status", "ready", False),
    ("/health", None, None),
)

RF05_CONTROL_POST_BOOTSTRAP = (
    "automation_status",
    "session_active",
    "env_loaded",
    "profile_dir",
    "whatsapp_url",
    "login_selector",
    "service",
)


@pytest.fixture(autouse=True)
def _reset_automation_engine() -> Generator[None, None, None]:
    reset_held_automation_session_for_tests()
    yield
    reset_held_automation_session_for_tests()


def test_rf05_dashboard_ui_available(client) -> None:
    response = client.get("/")

    assert response.status_code == 200
    assert "text/html" in (response.content_type or "")
    assert b"WhatsApp Web Automation" in response.data
    assert b"btn-launch" in response.data


def test_rf05_api_catalog_documents_control_endpoints(client) -> None:
    response = client.get("/api")

    assert response.status_code == 200
    payload = response.get_json()
    endpoints = payload["endpoints"]
    assert endpoints["automation_status"] == "/api/automation/status"
    assert endpoints["automation_start"] == "POST /api/automation/start"
    assert endpoints["automation_stop"] == "POST /api/automation/stop"
    assert payload["whatsapp_url"] == WA_URL
    assert payload["env_loaded"] is True
    assert payload["ui"] == "/painel"


@pytest.mark.parametrize("route,expected_status,session_active", RF05_CONTROL_GET_ROUTES)
def test_rf05_control_get_endpoints_operational(
    client,
    route: str,
    expected_status: str | None,
    session_active: bool | None,
) -> None:
    response = client.get(route)

    assert response.status_code == 200
    payload = response.get_json()
    if route == "/health":
        assert payload == {"status": "ok"}
        return
    assert payload["automation_status"] == expected_status
    assert payload["session_active"] is session_active
    assert payload["env_loaded"] is True
    assert payload["profile_dir"] == "profile_whatsapp_test"


def test_rf05_automation_start_prepares_engine_without_browser(client) -> None:
    response = client.post("/api/automation/start")

    assert response.status_code == 200
    payload = response.get_json()
    for field in RF05_CONTROL_POST_BOOTSTRAP:
        assert field in payload
    assert payload["automation_status"] == "ready"
    assert payload["session_active"] is False
    assert payload["login_selector"] == WHATSAPP_LOGIN_SELECTOR
    assert payload["whatsapp_url"] == WA_URL
    assert automation_is_running() is False


def test_rf05_automation_start_blocked_without_env(tmp_path: Path) -> None:
    from app import create_app

    missing_env = tmp_path / "missing.env"
    missing_env.write_text("WA_HEADLESS=true\n", encoding="utf-8")
    blocked = create_app(env_file=missing_env).test_client()

    response = blocked.post("/api/automation/start")

    assert response.status_code == 400
    assert response.get_json()["automation_status"] == "failed"
    assert automation_is_running() is False


def test_rf05_automation_stop_idempotent_when_idle(client) -> None:
    response = client.post("/api/automation/stop")

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["ok"] is True
    assert payload["stopped"] is False
    assert payload["session_active"] is False
    assert payload["automation_status"] == "ready"


def test_rf05_full_control_cycle_integrated_with_automation_engine(
    client,
    mock_playwright_stack_with_cleanup_tracking,
) -> None:
    """Bootstrap → launch (motor) → status → stop — sem sessão pendente."""
    _, mock_playwright, mock_context, mock_page, cleanup = (
        mock_playwright_stack_with_cleanup_tracking()
    )

    bootstrap = client.post("/api/automation/start")
    assert bootstrap.status_code == 200
    assert bootstrap.get_json()["automation_status"] == "ready"
    assert bootstrap.get_json()["session_active"] is False

    launch = client.post("/api/automation/start", json={"launch": True})
    assert launch.status_code == 200
    launch_payload = launch.get_json()
    assert launch_payload["automation_status"] == "running"
    assert launch_payload["session_active"] is True
    assert automation_is_running() is True
    mock_page.goto.assert_awaited_once()

    status = client.get("/api/automation/status")
    assert status.status_code == 200
    status_payload = status.get_json()
    assert status_payload["session_active"] is True
    assert status_payload["automation_status"] == "running"
    assert status_payload["headless"] is True

    stop = client.post("/api/automation/stop")
    assert stop.status_code == 200
    stop_payload = stop.get_json()
    assert stop_payload["ok"] is True
    assert stop_payload["stopped"] is True
    assert stop_payload["session_active"] is False
    assert stop_payload["automation_status"] == "ready"
    assert automation_is_running() is False

    mock_context.close.assert_awaited()
    mock_playwright.stop.assert_awaited()
    assert cleanup["context_closed"] >= 1
    assert cleanup["playwright_stopped"] >= 1

    idle = client.get("/api/automation/status")
    assert idle.get_json()["session_active"] is False
    assert idle.get_json()["automation_status"] == "ready"


def test_rf05_launch_rejects_duplicate_session(
    client,
    mock_playwright_stack_with_cleanup_tracking,
) -> None:
    mock_playwright_stack_with_cleanup_tracking()

    first = client.post("/api/automation/start", json={"launch": True})
    assert first.status_code == 200

    second = client.post("/api/automation/start", json={"launch": True})
    assert second.status_code == 409
    assert second.get_json()["session_active"] is True

    stop = client.post("/api/automation/stop")
    assert stop.status_code == 200
    assert stop.get_json()["stopped"] is True
    assert automation_is_running() is False


def test_visible_launch_replaces_headless_session(
    client,
    mock_playwright_stack_with_cleanup_tracking,
) -> None:
    mock_playwright_stack_with_cleanup_tracking()

    headless = client.post("/api/automation/start", json={"launch": True})
    assert headless.status_code == 200
    assert headless.get_json()["headless"] is True

    visible = client.post("/api/automation/start", json={"launch": True, "visible": True})
    assert visible.status_code == 200
    payload = visible.get_json()
    assert payload["whatsapp_authorized"] is True
    assert payload["session_active"] is False

    client.post("/api/automation/stop")


@pytest.mark.asyncio
async def test_rf05_api_bootstrap_aligns_with_start_automation_service(
    client,
    env_file: Path,
    mock_playwright_stack,
) -> None:
    api_response = client.post("/api/automation/start")
    assert api_response.status_code == 200
    assert api_response.get_json()["automation_status"] == "ready"

    _, mock_playwright, mock_context, _ = mock_playwright_stack()
    session = await start_automation(env_file)
    try:
        assert session.bootstrap.env_loaded is True
        assert session.page is not None
        assert session.bootstrap.config.headless is True
    finally:
        await mock_context.close()
        await mock_playwright.stop()
