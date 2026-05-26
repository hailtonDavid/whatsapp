"""Testes de fechamento automático do Edge após autorização."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from automation_service import (
    automation_is_running,
    ensure_whatsapp_authorized,
    probe_held_session_auth,
    reset_held_automation_session_for_tests,
    stop_automation,
)


@pytest.mark.asyncio
async def test_ensure_auth_closes_edge_when_authorized_headless(
    env_file: Path,
    mock_playwright_stack_with_cleanup_tracking,
) -> None:
    mock_playwright_stack_with_cleanup_tracking()
    reset_held_automation_session_for_tests()

    with patch(
        "automation_service.wait_for_stable_session_state",
        AsyncMock(return_value="logged_in"),
    ):
        auth = await ensure_whatsapp_authorized(env_file)

    assert auth["ok"] is True
    assert auth["session_active"] is False
    assert automation_is_running() is False


@pytest.mark.asyncio
async def test_probe_closes_visible_session_after_qr_scan(
    env_file: Path,
    mock_playwright_stack_with_cleanup_tracking,
) -> None:
    mock_playwright_stack_with_cleanup_tracking()
    reset_held_automation_session_for_tests()

    states = iter(["login_qr", "login_qr", "logged_in"])
    with patch(
        "automation_service.wait_for_stable_session_state",
        AsyncMock(side_effect=lambda *_args, **_kwargs: next(states)),
    ):
        await ensure_whatsapp_authorized(env_file)
        assert automation_is_running() is True

        auth = await probe_held_session_auth(env_file)

    assert auth["whatsapp_authorized"] is True
    assert auth["session_active"] is False
    assert automation_is_running() is False

    await stop_automation()
