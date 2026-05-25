"""Testes de integração Flask + Playwright com mocks (sem WhatsApp Web real)."""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from dotenv import load_dotenv

from app import WHATSAPP_LOGIN_SELECTOR, create_app
from browser_service import initialize_browser, wait_for_dynamic_ready, wait_for_login_element
from whatsapp_auto_downloader import AppConfig, load_app_config, open_whatsapp

pytestmark = pytest.mark.integration


@pytest.mark.asyncio
async def test_api_main_route_is_active(client) -> None:
    response = client.get("/")

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["status"] == "ok"
    assert payload["service"] == "whatsapp-web-automation"
    assert "whatsapp_url" in payload


@pytest.mark.asyncio
async def test_api_health_route_is_active(client) -> None:
    response = client.get("/health")

    assert response.status_code == 200
    assert response.get_json() == {"status": "ok"}


@pytest.mark.asyncio
async def test_flask_routes_active_within_async_loop(client) -> None:
    health = client.get("/health")
    index = client.get("/")

    assert health.status_code == 200
    assert index.status_code == 200
    assert health.get_json()["status"] == "ok"
    assert index.get_json()["status"] == "ok"
    assert index.get_json()["env_loaded"] is True


@pytest.mark.asyncio
async def test_reads_env_variables_from_dotenv(env_file: Path) -> None:
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


@pytest.mark.asyncio
async def test_flask_api_reflects_loaded_env(client) -> None:
    response = client.get("/")

    payload = response.get_json()
    assert payload["env_loaded"] is True
    assert payload["profile_dir"] == "profile_whatsapp_test"


@pytest.mark.asyncio
async def test_flask_and_browser_init_integration(
    client,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    env_file: Path,
) -> None:
    api_response = client.get("/")
    assert api_response.status_code == 200
    assert api_response.get_json()["profile_dir"] == "profile_whatsapp_test"

    config = load_app_config(env_file=env_file)

    mock_page = AsyncMock()
    mock_context = MagicMock()
    mock_context.pages = [mock_page]
    mock_context.new_page = AsyncMock(return_value=mock_page)

    mock_playwright = AsyncMock()
    mock_playwright.chromium.launch_persistent_context = AsyncMock(return_value=mock_context)

    mock_factory = MagicMock()
    mock_factory.start = AsyncMock(return_value=mock_playwright)
    monkeypatch.setattr("whatsapp_auto_downloader.async_playwright", lambda: mock_factory)

    playwright, context, page = await initialize_browser(config)

    mock_factory.start.assert_awaited_once()
    mock_page.goto.assert_awaited_once()
    assert playwright is mock_playwright
    assert context is mock_context
    assert page is mock_page
    assert config.profile_dir.name == "profile_whatsapp_test"


@pytest.mark.asyncio
async def test_playwright_browser_initialization_simulated(
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

    playwright, context, page = await initialize_browser(config)

    mock_factory.start.assert_awaited_once()
    mock_playwright.chromium.launch_persistent_context.assert_awaited_once()
    launch_kwargs = mock_playwright.chromium.launch_persistent_context.await_args.kwargs
    assert launch_kwargs["headless"] is True
    assert str(config.profile_dir) in launch_kwargs["user_data_dir"]
    mock_page.goto.assert_awaited_once()
    assert playwright is mock_playwright
    assert context is mock_context
    assert page is mock_page


@pytest.mark.asyncio
async def test_open_whatsapp_uses_empty_context_page_when_no_pages(
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

    _, _, page = await open_whatsapp(config)

    mock_context.new_page.assert_awaited_once()
    assert page is mock_page


@pytest.mark.asyncio
async def test_wait_for_login_element_uses_selector_mock() -> None:
    mock_page = AsyncMock()

    await wait_for_login_element(mock_page, timeout_seconds=45)

    mock_page.wait_for_selector.assert_awaited_once_with(
        WHATSAPP_LOGIN_SELECTOR,
        timeout=45_000,
        state="visible",
    )


@pytest.mark.asyncio
async def test_wait_for_dynamic_ready_delegates_to_wait_for_function() -> None:
    mock_page = AsyncMock()

    await wait_for_dynamic_ready(mock_page, timeout_seconds=25)

    mock_page.wait_for_function.assert_awaited_once()
    call_args = mock_page.wait_for_function.await_args
    assert "whatsapp" in call_args.args[0].lower()
    assert call_args.kwargs["timeout"] == 25_000


@pytest.mark.asyncio
async def test_wait_for_dynamic_ready_handles_timeout_without_crashing() -> None:
    from playwright.async_api import TimeoutError as PlaywrightTimeoutError

    mock_page = AsyncMock()
    mock_page.wait_for_function = AsyncMock(side_effect=PlaywrightTimeoutError("timeout"))

    await wait_for_dynamic_ready(mock_page, timeout_seconds=5)

    mock_page.wait_for_function.assert_awaited_once()
    assert mock_page.wait_for_function.await_args.kwargs["timeout"] == 5_000


@pytest.mark.asyncio
async def test_load_targets_config_accepts_send_block(tmp_path: Path) -> None:
    from whatsapp_auto_downloader import load_targets_config

    targets_file = tmp_path / "targets.json"
    targets_file.write_text(
        """
        {
          "targets": [
            {
              "id": "cliente_teste",
              "type": "contact",
              "name": "Cliente Teste",
              "enabled": true,
              "send": {
                "enabled": true,
                "message": "Mensagem de teste"
              }
            }
          ]
        }
        """,
        encoding="utf-8",
    )

    config = load_targets_config(targets_file)

    assert len(config.targets) == 1
    assert config.targets[0].id == "cliente_teste"
    assert config.targets[0].send_enabled is True
    assert config.targets[0].message == "Mensagem de teste"


@pytest.mark.asyncio
async def test_run_send_once_dry_run_does_not_use_browser(tmp_path: Path) -> None:
    from whatsapp_auto_downloader import AppConfig, Target, TargetsConfig, run_send_once

    app_config = AppConfig(
        profile_dir=tmp_path / "profile",
        headless=True,
        ready_timeout=30,
        export_dir=tmp_path / "exports",
        state_dir=tmp_path / "state",
    )
    targets_config = TargetsConfig(
        targets=[
            Target(
                id="cliente_teste",
                type="contact",
                name="Cliente Teste",
                enabled=True,
                send_enabled=True,
                message="Mensagem configurada",
            )
        ]
    )

    results = await run_send_once(
        page=None,
        app_config=app_config,
        targets_config=targets_config,
        dry_run=True,
    )

    assert len(results) == 1
    assert results[0]["dry_run"] is True
    assert results[0]["ok"] is True
    assert results[0]["message"] == "Mensagem configurada"
    assert (app_config.export_dir / "send" / "last_send.json").exists()
