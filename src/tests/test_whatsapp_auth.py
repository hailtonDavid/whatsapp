"""Testes de detecção de autorização e abertura automática do QR Code."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from automation_service import (
    connect_whatsapp_for_operation,
    ensure_whatsapp_authorized,
    is_whatsapp_authorized,
    reset_held_automation_session_for_tests,
    start_automation,
    stop_automation,
)


def test_is_whatsapp_authorized_only_when_logged_in() -> None:
    assert is_whatsapp_authorized("logged_in") is True
    assert is_whatsapp_authorized("login_qr") is False
    assert is_whatsapp_authorized("unknown") is False
    assert is_whatsapp_authorized(None) is False


@pytest.mark.asyncio
async def test_start_automation_keeps_headless_when_qr_detected(
    env_file: Path,
    mock_playwright_stack,
) -> None:
    mock_playwright_stack()

    with patch(
        "automation_service.wait_for_stable_session_state",
        AsyncMock(return_value="login_qr"),
    ):
        session = await start_automation(env_file, headless=True)

    assert session.bootstrap.config.headless is True
    assert session.session_state == "login_qr"


@pytest.mark.asyncio
async def test_ensure_whatsapp_authorized_opens_visible_for_qr(
    env_file: Path,
    mock_playwright_stack_with_cleanup_tracking,
) -> None:
    mock_playwright_stack_with_cleanup_tracking()
    reset_held_automation_session_for_tests()

    with patch(
        "automation_service.wait_for_stable_session_state",
        AsyncMock(return_value="login_qr"),
    ):
        outcome = await ensure_whatsapp_authorized(env_file)

    assert outcome["ok"] is False
    assert outcome["whatsapp_authorized"] is False
    assert outcome["session_state"] == "login_qr"
    assert outcome["session_active"] is True
    assert outcome["headless"] is False
    assert "QR Code" in outcome["message"]

    await stop_automation()


@pytest.mark.asyncio
async def test_connect_whatsapp_reuses_held_session_without_new_browser(
    env_file: Path,
    mock_playwright_stack_with_cleanup_tracking,
) -> None:
    mock_playwright_stack_with_cleanup_tracking()
    reset_held_automation_session_for_tests()

    with patch(
        "automation_service.wait_for_stable_session_state",
        AsyncMock(return_value="login_qr"),
    ):
        await ensure_whatsapp_authorized(env_file)

    open_mock = AsyncMock()
    with (
        patch("automation_service.open_whatsapp", open_mock),
        patch(
            "automation_service.wait_for_stable_session_state",
            AsyncMock(return_value="login_qr"),
        ),
    ):
        outcome = await connect_whatsapp_for_operation(env_file)

    open_mock.assert_not_awaited()
    assert isinstance(outcome, dict)
    assert outcome["requires_qr"] is True

    await stop_automation()


@pytest.mark.asyncio
async def test_connect_whatsapp_for_operation_keeps_visible_session_on_qr(
    env_file: Path,
    mock_playwright_stack_with_cleanup_tracking,
) -> None:
    mock_playwright_stack_with_cleanup_tracking()
    reset_held_automation_session_for_tests()

    with patch(
        "automation_service.resolve_session_state",
        AsyncMock(return_value="login_qr"),
    ):
        outcome = await connect_whatsapp_for_operation(env_file)

    assert isinstance(outcome, dict)
    assert outcome["ok"] is False
    assert outcome["requires_qr"] is True
    assert outcome["session_active"] is False


def test_api_status_includes_auth_fields(client) -> None:
    response = client.get("/api/automation/status")
    payload = response.get_json()
    assert response.status_code == 200
    assert "whatsapp_authorized" in payload
    assert "session_state" in payload
    assert payload["whatsapp_authorized"] is None


def test_api_ensure_auth_when_already_logged_in(
    client,
    mock_playwright_stack_with_cleanup_tracking,
) -> None:
    mock_playwright_stack_with_cleanup_tracking()

    response = client.post("/api/automation/ensure-auth")
    payload = response.get_json()

    assert response.status_code == 200
    assert payload["ok"] is True
    assert payload["whatsapp_authorized"] is True
    assert payload["session_state"] == "logged_in"
    assert payload["session_active"] is False


def test_api_launch_auto_reports_qr_state(
    client,
    mock_playwright_stack_with_cleanup_tracking,
) -> None:
    mock_playwright_stack_with_cleanup_tracking()

    with patch(
        "automation_service.wait_for_stable_session_state",
        AsyncMock(return_value="login_qr"),
    ):
        launch = client.post("/api/automation/start", json={"launch": True})

    payload = launch.get_json()
    assert launch.status_code == 200
    assert payload["session_active"] is True
    assert payload["whatsapp_authorized"] is False
    assert payload["headless"] is True

    stop = client.post("/api/automation/stop")
    assert stop.status_code == 200
