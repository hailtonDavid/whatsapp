"""Fixtures compartilhadas para testes da aplicação Flask."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from playwright.async_api import Page, async_playwright

from app import create_app


@pytest.fixture
def env_file(tmp_path: Path) -> Path:
    path = tmp_path / ".env"
    path.write_text(
        "\n".join(
            [
                "WA_PROFILE_DIR=profile_whatsapp_test",
                "WA_HEADLESS=true",
                "WA_READY_TIMEOUT=30",
                "WA_EXPORT_DIR=exports",
                "WA_STATE_DIR=state",
            ]
        ),
        encoding="utf-8",
    )
    return path


@pytest.fixture
def app(env_file: Path):
    return create_app(env_file=env_file)


@pytest.fixture
def client(app):
    return app.test_client()


@pytest.fixture
async def async_page() -> Page:
    """Página Playwright assíncrona (API async) para testes E2E."""
    channel = os.getenv("WA_BROWSER_CHANNEL", "msedge")
    playwright = await async_playwright().start()
    browser = await playwright.chromium.launch(headless=True, channel=channel)
    page = await browser.new_page()
    try:
        yield page
    finally:
        await browser.close()
        await playwright.stop()
