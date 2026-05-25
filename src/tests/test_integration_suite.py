"""Suíte de integração RF01 / RF05 / RF06 — Flask + Playwright."""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from dotenv import load_dotenv
from playwright.async_api import Page

from app import WHATSAPP_LOGIN_SELECTOR, WA_URL
from automation_service import (
    detect_qr_code_login,
    load_env_before_browser,
    start_automation,
)
from browser_service import wait_for_login_element
from whatsapp_auto_downloader import WA_URL as DOWNLOADER_WA_URL


pytestmark = [pytest.mark.integration]


# ---------------------------------------------------------------------------
# RF01 — .env carregado antes da inicialização do browser
# ---------------------------------------------------------------------------


@pytest.mark.rf01
@pytest.mark.asyncio
async def test_rf01_env_loaded_before_browser_init(
    env_file: Path,
    mock_playwright_stack,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    call_order: list[str] = []

    real_load_dotenv = load_dotenv

    def tracked_load(*args, **kwargs):
        call_order.append("env")
        return real_load_dotenv(*args, **kwargs)

    monkeypatch.setattr("automation_service.load_dotenv", tracked_load)

    mock_factory, mock_playwright, _, _ = mock_playwright_stack()

    async def tracked_start():
        call_order.append("playwright")
        return mock_playwright

    mock_factory.start = AsyncMock(side_effect=tracked_start)

    await start_automation(env_file)

    assert call_order.index("env") < call_order.index("playwright")
    assert os.getenv("WA_PROFILE_DIR") == "profile_whatsapp_test"


@pytest.mark.rf01
@pytest.mark.asyncio
async def test_rf01_start_automation_requires_env_before_goto(
    env_file: Path,
    mock_playwright_stack,
) -> None:
    mock_factory, _, _, mock_page = mock_playwright_stack()

    session = await start_automation(env_file)

    assert session.bootstrap.env_loaded is True
    assert os.getenv("WA_PROFILE_DIR") == "profile_whatsapp_test"
    mock_factory.start.assert_awaited_once()
    mock_page.goto.assert_awaited_once_with(DOWNLOADER_WA_URL, wait_until="domcontentloaded")


@pytest.mark.rf01
def test_rf01_flask_blocks_automation_without_env(tmp_path: Path) -> None:
    from app import create_app

    missing_env = tmp_path / "missing.env"
    missing_env.write_text("WA_HEADLESS=true\n", encoding="utf-8")

    app = create_app(env_file=missing_env)
    client = app.test_client()

    response = client.post("/api/automation/start")

    assert response.status_code == 400
    assert response.get_json()["automation_status"] == "failed"


# ---------------------------------------------------------------------------
# RF05 — Flask inicia corretamente a automação
# ---------------------------------------------------------------------------


@pytest.mark.rf05
@pytest.mark.asyncio
async def test_rf05_flask_automation_status_endpoint(client) -> None:
    response = client.get("/api/automation/status")

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["automation_status"] == "ready"
    assert payload["env_loaded"] is True
    assert payload["profile_dir"] == "profile_whatsapp_test"
    assert payload["whatsapp_url"] == WA_URL


@pytest.mark.rf05
@pytest.mark.asyncio
async def test_rf05_flask_starts_automation_via_api(client) -> None:
    response = client.post("/api/automation/start")

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["automation_status"] == "ready"
    assert payload["service"] == "whatsapp-web-automation"
    assert payload["env_loaded"] is True
    assert payload["profile_dir"] == "profile_whatsapp_test"
    assert payload["login_selector"] == WHATSAPP_LOGIN_SELECTOR
    assert payload["whatsapp_url"] == WA_URL


@pytest.mark.rf05
@pytest.mark.asyncio
async def test_rf05_flask_and_playwright_bootstrap_integration(
    client,
    env_file: Path,
    mock_playwright_stack,
) -> None:
    api_response = client.post("/api/automation/start")
    assert api_response.status_code == 200
    assert api_response.get_json()["automation_status"] == "ready"

    session = await start_automation(env_file)

    assert session.bootstrap.config.headless is True
    assert session.page is not None


# ---------------------------------------------------------------------------
# RF06 — Playwright identifica seletor de QR Code
# ---------------------------------------------------------------------------


@pytest.mark.rf06
@pytest.mark.asyncio
async def test_rf06_playwright_detects_qr_selector_on_mock_page(
    async_page: Page,
    qr_login_html: str,
) -> None:
    await async_page.set_content(qr_login_html)

    detected = await detect_qr_code_login(async_page, timeout_seconds=5)

    assert detected is True
    locator = async_page.locator(WHATSAPP_LOGIN_SELECTOR).first
    assert await locator.is_visible()


@pytest.mark.rf06
@pytest.mark.asyncio
async def test_rf06_wait_for_login_element_targets_qr_canvas(async_page: Page) -> None:
    await async_page.set_content(
        '<canvas aria-label="Scan this QR code to link a device!" role="img"></canvas>'
    )

    await wait_for_login_element(async_page, timeout_seconds=5)

    assert await async_page.locator('canvas[aria-label*="QR" i]').is_visible()


@pytest.mark.rf06
@pytest.mark.browser
@pytest.mark.asyncio
async def test_rf06_playwright_detects_qr_on_whatsapp_web(async_page: Page) -> None:
    await async_page.goto(WA_URL, wait_until="domcontentloaded", timeout=60_000)

    detected = await detect_qr_code_login(async_page, timeout_seconds=60)

    assert detected is True
    assert await async_page.locator(WHATSAPP_LOGIN_SELECTOR).first.is_visible()
