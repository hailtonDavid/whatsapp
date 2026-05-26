"""Testes da aplicação Flask e E2E com WhatsApp Web."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from dotenv import load_dotenv
from playwright.async_api import Page

from app import create_app
from browser_service import wait_for_login_element
from wa_selectors import WHATSAPP_LOGIN_SELECTOR
from whatsapp_auto_downloader import WA_URL


def test_env_file_is_loaded(env_file: Path) -> None:
    load_dotenv(env_file, override=True)

    app = create_app(env_file=env_file)

    assert app.config["ENV_LOADED"] is True
    assert os.getenv("WA_PROFILE_DIR") == "profile_whatsapp_test"
    assert app.config["APP_CONFIG"].profile_dir.name == "profile_whatsapp_test"
    assert app.config["APP_CONFIG"].headless is True


@pytest.mark.asyncio
async def test_index_returns_html_dashboard(client) -> None:
    response = client.get("/")

    assert response.status_code == 200
    assert "text/html" in (response.content_type or "")
    assert b"WhatsApp Web Automation" in response.data


@pytest.mark.asyncio
async def test_api_index_returns_json(client) -> None:
    response = client.get("/api")

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["status"] == "ok"
    assert payload["env_loaded"] is True
    assert payload["service"] == "whatsapp-web-automation"


@pytest.mark.browser
@pytest.mark.asyncio
async def test_whatsapp_web_login_selector(async_page: Page, diagnostics_dir) -> None:
    await async_page.goto(WA_URL, wait_until="domcontentloaded", timeout=60_000)
    await wait_for_login_element(async_page, timeout_seconds=60, diagnostics_dir=diagnostics_dir)

    login_locator = async_page.locator(WHATSAPP_LOGIN_SELECTOR).first
    assert await login_locator.is_visible()
