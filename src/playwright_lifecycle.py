"""Teardown ordenado de recursos Playwright (Windows ProactorEventLoop)."""

from __future__ import annotations

import asyncio
import gc
import sys
from typing import Iterable

from playwright.async_api import Browser, BrowserContext, Page, Playwright

_WINDOWS_SUBPROCESS_DRAIN_SECONDS = 0.35


async def _page_is_closed(page: Page) -> bool:
    try:
        closed = page.is_closed()
        if asyncio.iscoroutine(closed):
            closed = await closed
        return bool(closed)
    except Exception:
        return False


async def _close_pages(pages: Iterable[Page]) -> None:
    for page in pages:
        try:
            if await _page_is_closed(page):
                continue
            await page.close()
        except Exception:
            pass


async def _close_context(context: BrowserContext | None) -> None:
    if context is None:
        return
    try:
        await _close_pages(context.pages)
        await context.close()
    except Exception:
        pass


async def _close_browser(browser: Browser | None) -> None:
    if browser is None:
        return
    try:
        if browser.is_connected():
            await browser.close()
    except Exception:
        pass


async def _stop_playwright(playwright: Playwright | None) -> None:
    if playwright is None:
        return
    try:
        await playwright.stop()
    except Exception:
        pass


async def drain_event_loop_subprocesses() -> None:
    """Aguarda subprocessos/pipes do ProactorEventLoop encerrarem no Windows."""
    if sys.platform != "win32":
        return
    await asyncio.sleep(_WINDOWS_SUBPROCESS_DRAIN_SECONDS)
    await asyncio.sleep(0)


async def shutdown_playwright_stack(
    *,
    pages: Iterable[Page] | None = None,
    context: BrowserContext | None = None,
    browser: Browser | None = None,
    playwright: Playwright | None = None,
) -> None:
    """Fecha pages → context → browser → playwright, depois drena o event loop."""
    if pages is not None:
        await _close_pages(pages)
    elif context is not None:
        await _close_pages(context.pages)

    await _close_context(context)
    await _close_browser(browser)
    await _stop_playwright(playwright)
    await drain_event_loop_subprocesses()
    if sys.platform == "win32":
        gc.collect()
        await asyncio.sleep(0)
