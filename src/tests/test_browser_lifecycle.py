"""Testes de ciclo de vida Playwright — cleanup garantido mesmo em falha."""

from __future__ import annotations

import os
import sys

import pytest
from playwright.async_api import Browser, Page, Playwright

from playwright_lifecycle import drain_event_loop_subprocesses

pytestmark = [pytest.mark.integration]


@pytest.mark.asyncio
async def test_playwright_engine_stops_after_test_failure(playwright_engine: Playwright) -> None:
    assert playwright_engine.chromium is not None
    with pytest.raises(RuntimeError, match="falha simulada"):
        raise RuntimeError("falha simulada")


@pytest.mark.asyncio
async def test_browser_closes_after_test_failure(browser: Browser) -> None:
    page = await browser.new_page()
    await page.goto("about:blank")
    with pytest.raises(ValueError, match="falha simulada"):
        raise ValueError("falha simulada")


@pytest.mark.asyncio
async def test_async_page_closes_after_test_failure(async_page: Page) -> None:
    await async_page.goto("about:blank")
    with pytest.raises(AssertionError, match="falha simulada"):
        raise AssertionError("falha simulada")


@pytest.mark.asyncio
async def test_tracked_browser_records_close_on_teardown(tracked_browser: dict) -> None:
    page = await tracked_browser["browser"].new_page()
    await page.goto("about:blank")
    with pytest.raises(RuntimeError, match="falha simulada"):
        raise RuntimeError("falha simulada")
    await tracked_browser["browser"].close()
    assert tracked_browser["close_calls"] == 1


@pytest.mark.asyncio
async def test_managed_playwright_resources_close_all_on_failure(
    managed_playwright_resources,
) -> None:
    resources = managed_playwright_resources
    channel = os.getenv("WA_BROWSER_CHANNEL", "msedge")
    resources.browser = await resources.playwright.chromium.launch(headless=True, channel=channel)
    page = await resources.browser.new_page()
    resources.pages.append(page)
    await page.goto("about:blank")

    with pytest.raises(RuntimeError, match="falha simulada"):
        raise RuntimeError("falha simulada")

    await resources.close_all()
    assert resources.closed is True


@pytest.mark.asyncio
async def test_mock_stack_tracks_context_and_playwright_cleanup(
    mock_playwright_stack_with_cleanup_tracking,
    env_file,
) -> None:
    from automation_service import start_automation

    _, mock_playwright, mock_context, _, cleanup = mock_playwright_stack_with_cleanup_tracking()
    session = await start_automation(env_file)

    await session.context.close()
    await session.playwright.stop()

    assert cleanup["context_closed"] == 1
    assert cleanup["playwright_stopped"] == 1
    mock_context.close.assert_awaited()
    mock_playwright.stop.assert_awaited()


@pytest.mark.asyncio
@pytest.mark.skipif(sys.platform != "win32", reason="Validação específica do ProactorEventLoop no Windows.")
async def test_drain_event_loop_subprocesses_completes_on_windows() -> None:
    await drain_event_loop_subprocesses()
