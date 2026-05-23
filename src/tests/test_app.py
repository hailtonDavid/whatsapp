"""Testes da aplicação Flask e E2E com WhatsApp Web."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from dotenv import load_dotenv
from playwright.async_api import Page

from app import WHATSAPP_LOGIN_SELECTOR, WA_URL, create_app


def test_env_file_is_loaded(env_file: Path) -> None:
    load_dotenv(env_file, override=True)

    app = create_app(env_file=env_file)

    assert app.config["ENV_LOADED"] is True
    assert os.getenv("WA_PROFILE_DIR") == "profile_whatsapp_test"
    assert app.config["APP_CONFIG"].profile_dir.name == "profile_whatsapp_test"
    assert app.config["APP_CONFIG"].headless is True


@pytest.mark.asyncio
async def test_index_returns_200_ok(client) -> None:
    response = client.get("/")

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["status"] == "ok"
    assert payload["env_loaded"] is True
    assert payload["service"] == "whatsapp-web-automation"


@pytest.mark.browser
@pytest.mark.asyncio
async def test_whatsapp_web_login_selector(async_page: Page) -> None:
    await async_page.goto(WA_URL, wait_until="domcontentloaded", timeout=60_000)
    await async_page.wait_for_selector(WHATSAPP_LOGIN_SELECTOR, timeout=60_000, state="visible")

    login_locator = async_page.locator(WHATSAPP_LOGIN_SELECTOR).first
    assert await login_locator.is_visible()
