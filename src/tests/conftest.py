"""Fixtures compartilhadas para testes da aplicação Flask."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from playwright.async_api import Page, async_playwright

from app import create_app


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--skip-ruff",
        action="store_true",
        default=False,
        help="Pula verificação Ruff antes dos testes.",
    )


def pytest_sessionstart(session: pytest.Session) -> None:
    if session.config.getoption("--skip-ruff"):
        return

    project_root = Path(session.config.rootpath)
    src_dir = project_root / "src"
    if not src_dir.is_dir():
        return

    result = subprocess.run(
        [sys.executable, "-m", "ruff", "check", str(src_dir)],
        capture_output=True,
        text=True,
        cwd=str(project_root),
    )
    if result.returncode != 0:
        output = (result.stdout or "") + (result.stderr or "")
        pytest.exit(f"Ruff lint falhou antes dos testes:\n{output}", returncode=result.returncode)


@pytest.fixture(autouse=True)
def reset_wa_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in (
        "WA_PROFILE_DIR",
        "WA_HEADLESS",
        "WA_READY_TIMEOUT",
        "WA_EXPORT_DIR",
        "WA_STATE_DIR",
    ):
        monkeypatch.delenv(key, raising=False)


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
def mock_playwright_stack(monkeypatch: pytest.MonkeyPatch):
    """Stack Playwright mockado para testes de integração offline."""

    def _factory(*, pages: list | None = None):
        mock_page = AsyncMock()
        mock_context = MagicMock()
        mock_context.pages = pages if pages is not None else [mock_page]
        mock_context.new_page = AsyncMock(return_value=mock_page)

        mock_playwright = AsyncMock()
        mock_playwright.chromium.launch_persistent_context = AsyncMock(return_value=mock_context)
        mock_playwright.stop = AsyncMock()

        mock_factory = MagicMock()
        mock_factory.start = AsyncMock(return_value=mock_playwright)
        monkeypatch.setattr("whatsapp_auto_downloader.async_playwright", lambda: mock_factory)

        return mock_factory, mock_playwright, mock_context, mock_page

    return _factory


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


@pytest.fixture
def qr_login_html() -> str:
    return (
        '<!DOCTYPE html><html><body>'
        '<canvas aria-label="Scan this QR code to link a device!" role="img"></canvas>'
        '<div data-testid="link-device-qrcode-alt-linking-help">Precisa de ajuda?</div>'
        "</body></html>"
    )
