"""Testes de integração Flask + Playwright com mocks (sem WhatsApp Web real)."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from dotenv import load_dotenv

from app import WHATSAPP_LOGIN_SELECTOR, create_app
from browser_service import initialize_browser, wait_for_dynamic_ready, wait_for_login_element
from whatsapp_auto_downloader import AppConfig, load_app_config, open_whatsapp


pytestmark = pytest.mark.integration


def test_api_main_route_is_active(client) -> None:
    response = client.get("/")

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["status"] == "ok"
    assert payload["service"] == "whatsapp-web-automation"
    assert "whatsapp_url" in payload


def test_api_health_route_is_active(client) -> None:
    response = client.get("/health")

    assert response.status_code == 200
    assert response.get_json() == {"status": "ok"}


def test_reads_env_variables_from_dotenv(env_file: Path) -> None:
    load_dotenv(env_file, override=True)
    config = load_app_config(env_file=env_file)
    app = create_app(env_file=env_file)

    assert os.getenv("WA_PROFILE_DIR") == "profile_whatsapp_test"
    assert os.getenv("WA_HEADLESS") == "true"
    assert os.getenv("WA_READY_TIMEOUT") == "30"
    assert config.profile_dir.name == "profile_whatsapp_test"
    assert config.headless is True
    assert config.ready_timeout == 30
    assert app.config["ENV_LOADED"] is True
    assert app.config["ENV_FILE"] == str(env_file)


def test_flask_api_reflects_loaded_env(client) -> None:
    response = client.get("/")

    payload = response.get_json()
    assert payload["env_loaded"] is True
    assert payload["profile_dir"] == "profile_whatsapp_test"


def test_playwright_browser_initialization_simulated(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    config = AppConfig(
        profile_dir=tmp_path / "profile",
        headless=True,
        ready_timeout=30,
        export_dir=tmp_path / "exports",
        state_dir=tmp_path / "state",
    )

    mock_page = AsyncMock()
    mock_context = MagicMock()
    mock_context.pages = [mock_page]
    mock_context.new_page = AsyncMock(return_value=mock_page)

    mock_playwright = AsyncMock()
    mock_playwright.chromium.launch_persistent_context = AsyncMock(return_value=mock_context)
    mock_playwright.stop = AsyncMock()

    mock_factory = MagicMock()
    mock_factory.start = AsyncMock(return_value=mock_playwright)

    monkeypatch.setattr("whatsapp_auto_downloader.async_playwright", lambda: mock_factory)

    playwright, context, page = asyncio.run(initialize_browser(config))

    mock_factory.start.assert_awaited_once()
    mock_playwright.chromium.launch_persistent_context.assert_awaited_once()
    launch_kwargs = mock_playwright.chromium.launch_persistent_context.await_args.kwargs
    assert launch_kwargs["headless"] is True
    assert str(config.profile_dir) in launch_kwargs["user_data_dir"]
    mock_page.goto.assert_awaited_once()
    assert playwright is mock_playwright
    assert context is mock_context
    assert page is mock_page


def test_open_whatsapp_uses_empty_context_page_when_no_pages(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    config = AppConfig(
        profile_dir=tmp_path / "profile",
        headless=True,
        ready_timeout=30,
        export_dir=tmp_path / "exports",
        state_dir=tmp_path / "state",
    )

    mock_page = AsyncMock()
    mock_context = MagicMock()
    mock_context.pages = []
    mock_context.new_page = AsyncMock(return_value=mock_page)

    mock_playwright = AsyncMock()
    mock_playwright.chromium.launch_persistent_context = AsyncMock(return_value=mock_context)

    mock_factory = MagicMock()
    mock_factory.start = AsyncMock(return_value=mock_playwright)
    monkeypatch.setattr("whatsapp_auto_downloader.async_playwright", lambda: mock_factory)

    _, _, page = asyncio.run(open_whatsapp(config))

    mock_context.new_page.assert_awaited_once()
    assert page is mock_page


def test_wait_for_login_element_uses_selector_mock() -> None:
    mock_page = AsyncMock()

    asyncio.run(wait_for_login_element(mock_page, timeout_seconds=45))

    mock_page.wait_for_selector.assert_awaited_once_with(
        WHATSAPP_LOGIN_SELECTOR,
        timeout=45_000,
        state="visible",
    )


def test_wait_for_dynamic_ready_delegates_to_wait_for_function() -> None:
    mock_page = AsyncMock()

    asyncio.run(wait_for_dynamic_ready(mock_page, timeout_seconds=25))

    mock_page.wait_for_function.assert_awaited_once()
    call_args = mock_page.wait_for_function.await_args
    assert "whatsapp" in call_args.args[0].lower()
    assert call_args.kwargs["timeout"] == 25_000


def test_wait_for_dynamic_ready_handles_timeout_without_crashing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from playwright.async_api import TimeoutError as PlaywrightTimeoutError

    mock_page = AsyncMock()
    mock_page.wait_for_function = AsyncMock(side_effect=PlaywrightTimeoutError("timeout"))

    asyncio.run(wait_for_dynamic_ready(mock_page, timeout_seconds=5))

    mock_page.wait_for_function.assert_awaited_once()
    assert mock_page.wait_for_function.await_args.kwargs["timeout"] == 5_000
