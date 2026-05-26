"""Testes de diagnóstico RF06 — screenshot e DOM quando seletor dinâmico falha."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from browser_diagnostics import DynamicElementNotFoundError, wait_for_visible_selector
from browser_service import wait_for_login_element
from wa_selectors import WHATSAPP_LOGIN_SELECTOR

pytestmark = [pytest.mark.integration, pytest.mark.rf06]


@pytest.mark.asyncio
async def test_wait_for_visible_selector_success() -> None:
    mock_page = AsyncMock()
    mock_page.wait_for_selector = AsyncMock()

    await wait_for_visible_selector(
        mock_page,
        WHATSAPP_LOGIN_SELECTOR,
        timeout_seconds=5,
        label="rf06_login",
    )

    mock_page.wait_for_selector.assert_awaited_once_with(
        WHATSAPP_LOGIN_SELECTOR,
        timeout=5_000,
        state="visible",
    )


@pytest.mark.asyncio
async def test_wait_for_visible_selector_captures_diagnostics_on_timeout(
    diagnostics_dir,
) -> None:
    mock_page = AsyncMock()
    mock_page.wait_for_selector = AsyncMock(side_effect=PlaywrightTimeoutError("timeout"))
    mock_page.url = "https://web.whatsapp.com/"
    mock_page.title = AsyncMock(return_value="WhatsApp")
    mock_page.evaluate = AsyncMock(
        return_value={
            "body_text_preview": "WhatsApp",
            "selector_matches": 0,
            "canvas_count": 0,
        }
    )
    mock_page.screenshot = AsyncMock()
    mock_page.content = AsyncMock(return_value="<html><body>WhatsApp</body></html>")

    with pytest.raises(DynamicElementNotFoundError) as exc_info:
        await wait_for_visible_selector(
            mock_page,
            WHATSAPP_LOGIN_SELECTOR,
            timeout_seconds=3,
            label="rf06_login",
            diagnostics_dir=diagnostics_dir,
        )

    err = exc_info.value
    assert err.selector == WHATSAPP_LOGIN_SELECTOR
    assert err.label == "rf06_login"
    assert err.timeout_seconds == 3
    assert err.diagnostics["url"] == "https://web.whatsapp.com/"
    assert err.diagnostics["dom"]["selector_matches"] == 0
    assert "screenshot" in err.diagnostics
    assert "html_dump" in err.diagnostics
    mock_page.screenshot.assert_awaited_once()
    mock_page.content.assert_awaited_once()


@pytest.mark.asyncio
async def test_wait_for_login_element_delegates_to_diagnostics_policy(diagnostics_dir) -> None:
    mock_page = AsyncMock()
    mock_page.wait_for_selector = AsyncMock()

    await wait_for_login_element(mock_page, timeout_seconds=10, diagnostics_dir=diagnostics_dir)

    mock_page.wait_for_selector.assert_awaited_once_with(
        WHATSAPP_LOGIN_SELECTOR,
        timeout=10_000,
        state="visible",
    )


@pytest.mark.asyncio
async def test_wait_for_login_element_raises_structured_error_not_raw_timeout(
    diagnostics_dir,
) -> None:
    mock_page = AsyncMock()
    mock_page.wait_for_selector = AsyncMock(side_effect=PlaywrightTimeoutError("timeout"))
    mock_page.url = "https://web.whatsapp.com/"
    mock_page.title = AsyncMock(return_value="WhatsApp")
    mock_page.evaluate = AsyncMock(return_value={"selector_matches": 0})
    mock_page.screenshot = AsyncMock()
    mock_page.content = AsyncMock(return_value="<html></html>")

    with pytest.raises(DynamicElementNotFoundError) as exc_info:
        await wait_for_login_element(mock_page, timeout_seconds=2, diagnostics_dir=diagnostics_dir)

    assert exc_info.value.label == "rf06_login"
    assert "rf06_login" in str(exc_info.value)
