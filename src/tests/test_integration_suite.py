"""Suíte de integração RF01 / RF06 — Flask + Playwright com teardown explícito."""

from __future__ import annotations

import os
from collections.abc import AsyncGenerator
from dataclasses import dataclass, field
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from dotenv import load_dotenv
from playwright.async_api import Browser, BrowserContext, Page, Playwright, async_playwright

from app import create_app
from automation_service import detect_qr_code_login, start_automation
from browser_service import wait_for_login_element
from playwright_lifecycle import drain_event_loop_subprocesses
from wa_selectors import WHATSAPP_LOGIN_SELECTOR
from whatsapp_auto_downloader import WA_URL

pytestmark = [pytest.mark.integration]


# ---------------------------------------------------------------------------
# Helpers de fechamento seguro (Windows / ProactorEventLoop)
# ---------------------------------------------------------------------------


async def _close_page_safe(page: Page | None) -> None:
    if page is None:
        return
    try:
        closed = page.is_closed()
        if hasattr(closed, "__await__"):
            closed = await closed
        if not closed:
            await page.close()
    except Exception:
        pass


async def _close_context_safe(context: BrowserContext | None) -> None:
    if context is None:
        return
    try:
        for page in list(context.pages):
            await _close_page_safe(page)
        await context.close()
    except Exception:
        pass


async def _close_browser_safe(browser: Browser | None) -> None:
    if browser is None:
        return
    try:
        if browser.is_connected():
            await browser.close()
    except Exception:
        pass


async def _stop_playwright_safe(playwright: Playwright | None) -> None:
    if playwright is None:
        return
    try:
        await playwright.stop()
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Fixtures Playwright — try/finally em cada camada + shutdown final do stack
# ---------------------------------------------------------------------------


@dataclass
class SuitePlaywrightStack:
    """Recursos da suíte; cada fixture libera sua camada no finally."""

    playwright: Playwright | None = None
    browser: Browser | None = None
    context: BrowserContext | None = None
    pages: list[Page] = field(default_factory=list)
    playwright_stopped: bool = False

    async def shutdown_remaining(self) -> None:
        """Encerra apenas o que ainda não foi fechado pelas fixtures filhas."""
        for page in reversed(list(self.pages)):
            await _close_page_safe(page)
        self.pages.clear()

        if self.context is not None:
            await _close_context_safe(self.context)
            self.context = None

        if self.browser is not None:
            await _close_browser_safe(self.browser)
            self.browser = None

        if not self.playwright_stopped and self.playwright is not None:
            await _stop_playwright_safe(self.playwright)
            self.playwright_stopped = True
            await drain_event_loop_subprocesses()

        self.playwright = None


@pytest.fixture
async def suite_playwright_stack() -> AsyncGenerator[SuitePlaywrightStack, None]:
    stack = SuitePlaywrightStack()
    playwright: Playwright | None = None
    try:
        playwright = await async_playwright().start()
        stack.playwright = playwright
        yield stack
    finally:
        try:
            await stack.shutdown_remaining()
        finally:
            stack.playwright = None


@pytest.fixture
async def suite_browser(
    suite_playwright_stack: SuitePlaywrightStack,
) -> AsyncGenerator[Browser, None]:
    if suite_playwright_stack.playwright is None:
        pytest.fail("Playwright não inicializado na fixture suite_playwright_stack.")

    browser: Browser | None = None
    channel = os.getenv("WA_BROWSER_CHANNEL", "msedge")
    try:
        browser = await suite_playwright_stack.playwright.chromium.launch(
            headless=True,
            channel=channel,
        )
        suite_playwright_stack.browser = browser
        yield browser
    finally:
        try:
            await _close_browser_safe(browser)
        finally:
            if browser is not None and suite_playwright_stack.browser is browser:
                suite_playwright_stack.browser = None


@pytest.fixture
async def suite_browser_context(
    suite_browser: Browser,
    suite_playwright_stack: SuitePlaywrightStack,
) -> AsyncGenerator[BrowserContext, None]:
    context: BrowserContext | None = None
    try:
        context = await suite_browser.new_context()
        suite_playwright_stack.context = context
        yield context
    finally:
        try:
            await _close_context_safe(context)
        finally:
            if context is not None and suite_playwright_stack.context is context:
                suite_playwright_stack.context = None


@pytest.fixture
async def suite_async_page(
    suite_browser_context: BrowserContext,
    suite_playwright_stack: SuitePlaywrightStack,
) -> AsyncGenerator[Page, None]:
    page: Page | None = None
    try:
        page = await suite_browser_context.new_page()
        suite_playwright_stack.pages.append(page)
        yield page
    finally:
        try:
            await _close_page_safe(page)
        finally:
            if page is not None and page in suite_playwright_stack.pages:
                suite_playwright_stack.pages.remove(page)


@pytest.fixture
def mock_playwright_stack(monkeypatch: pytest.MonkeyPatch):
    """Stack Playwright mockado para testes offline."""

    def _factory(*, pages: list | None = None):
        mock_page = AsyncMock()
        mock_page.is_closed = MagicMock(return_value=False)
        mock_context = AsyncMock()
        mock_context.pages = pages if pages is not None else [mock_page]
        mock_context.new_page = AsyncMock(return_value=mock_page)
        mock_context.close = AsyncMock()

        mock_playwright = AsyncMock()
        mock_playwright.chromium.launch_persistent_context = AsyncMock(return_value=mock_context)
        mock_playwright.stop = AsyncMock()

        mock_factory = MagicMock()
        mock_factory.start = AsyncMock(return_value=mock_playwright)
        monkeypatch.setattr("whatsapp_auto_downloader.async_playwright", lambda: mock_factory)

        return mock_factory, mock_playwright, mock_context, mock_page

    return _factory


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

    mock_factory, mock_playwright, mock_context, _ = mock_playwright_stack()

    async def tracked_start():
        call_order.append("playwright")
        return mock_playwright

    mock_factory.start = AsyncMock(side_effect=tracked_start)

    session = await start_automation(env_file)
    try:
        assert session.bootstrap.env_loaded is True
        assert call_order.index("env") < call_order.index("playwright")
        assert os.getenv("WA_PROFILE_DIR") == "profile_whatsapp_test"
    finally:
        await mock_context.close()
        await mock_playwright.stop()


@pytest.mark.rf01
@pytest.mark.asyncio
async def test_rf01_start_automation_requires_env_before_goto(
    env_file: Path,
    mock_playwright_stack,
) -> None:
    mock_factory, mock_playwright, mock_context, mock_page = mock_playwright_stack()

    session = await start_automation(env_file)
    try:
        assert session.bootstrap.env_loaded is True
        assert os.getenv("WA_PROFILE_DIR") == "profile_whatsapp_test"
        mock_factory.start.assert_awaited_once()
        mock_page.goto.assert_awaited_once_with(WA_URL, wait_until="domcontentloaded")
    finally:
        await mock_context.close()
        await mock_playwright.stop()


@pytest.mark.rf01
def test_rf01_flask_blocks_automation_without_env(tmp_path: Path) -> None:
    missing_env = tmp_path / "missing.env"
    missing_env.write_text("WA_HEADLESS=true\n", encoding="utf-8")

    app = create_app(env_file=missing_env)
    client = app.test_client()

    response = client.post("/api/automation/start")

    assert response.status_code == 400
    assert response.get_json()["automation_status"] == "failed"


# ---------------------------------------------------------------------------
# RF06 — Playwright identifica seletor de QR Code
# ---------------------------------------------------------------------------


@pytest.mark.rf06
@pytest.mark.asyncio
async def test_rf06_playwright_detects_qr_selector_on_mock_page() -> None:
    mock_page = AsyncMock()
    mock_page.wait_for_selector = AsyncMock()
    mock_locator = AsyncMock()
    mock_locator.is_visible = AsyncMock(return_value=True)
    mock_page.locator.return_value.first = mock_locator

    detected = await detect_qr_code_login(mock_page, timeout_seconds=5)

    assert detected is True
    mock_page.wait_for_selector.assert_awaited_once()
    call_kwargs = mock_page.wait_for_selector.await_args.kwargs
    assert call_kwargs["timeout"] == 5_000
    assert call_kwargs["state"] == "visible"


@pytest.mark.rf06
@pytest.mark.asyncio
async def test_rf06_wait_for_login_element_targets_qr_canvas() -> None:
    mock_page = AsyncMock()
    mock_page.wait_for_selector = AsyncMock()
    mock_page.locator.return_value.is_visible = AsyncMock(return_value=True)

    await wait_for_login_element(mock_page, timeout_seconds=5)

    mock_page.wait_for_selector.assert_awaited_once()
    call_kwargs = mock_page.wait_for_selector.await_args.kwargs
    assert call_kwargs["timeout"] == 5_000
    assert call_kwargs["state"] == "visible"


@pytest.mark.rf06
@pytest.mark.browser
@pytest.mark.asyncio
async def test_rf06_playwright_detects_qr_on_whatsapp_web(suite_async_page: Page) -> None:
    await suite_async_page.goto(WA_URL, wait_until="domcontentloaded", timeout=60_000)

    detected = await detect_qr_code_login(suite_async_page, timeout_seconds=60)

    assert detected is True
    assert await suite_async_page.locator(WHATSAPP_LOGIN_SELECTOR).first.is_visible()


@pytest.mark.rf06
@pytest.mark.browser
@pytest.mark.asyncio
async def test_rf06_suite_stack_closes_browser_after_failure(
    suite_browser: Browser,
    suite_playwright_stack: SuitePlaywrightStack,
) -> None:
    page = await suite_browser.new_page()
    try:
        await page.goto("about:blank")
        with pytest.raises(RuntimeError, match="falha simulada"):
            raise RuntimeError("falha simulada")
    finally:
        await _close_page_safe(page)

    assert suite_playwright_stack.browser is not None or suite_playwright_stack.playwright_stopped
