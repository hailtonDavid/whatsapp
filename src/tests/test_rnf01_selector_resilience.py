"""RNF01 — resiliência de seletores CSS (Page Object + fallbacks)."""

from __future__ import annotations

import pytest
from playwright.async_api import Page

from pages.base_page import SelectorGroup
from pages.whatsapp_login_page import WhatsAppLoginPage
from pages.whatsapp_main_page import WhatsAppMainPage
from wa_selectors import (
    WHATSAPP_LOGIN_SELECTOR,
    WHATSAPP_LOGIN_SELECTORS,
    WHATSAPP_MAIN_SELECTORS,
    WHATSAPP_MESSAGE_BOX_SELECTORS,
    WHATSAPP_SEARCH_SELECTORS,
)

pytestmark = [pytest.mark.integration, pytest.mark.rnf01]

SELECTOR_GROUPS = (
    WHATSAPP_LOGIN_SELECTORS,
    WHATSAPP_MAIN_SELECTORS,
    WHATSAPP_SEARCH_SELECTORS,
    WHATSAPP_MESSAGE_BOX_SELECTORS,
)


@pytest.mark.parametrize(
    "selectors",
    SELECTOR_GROUPS,
    ids=["login", "main", "search", "message_box"],
)
def test_rnf01_selector_groups_are_non_empty_and_unique(selectors: tuple[str, ...]) -> None:
    assert len(selectors) >= 2, "RNF01 exige ao menos 2 fallbacks por grupo"
    assert len(set(selectors)) == len(selectors)
    for selector in selectors:
        assert selector.strip()
        assert "  " not in selector


def test_rnf01_legacy_login_selector_matches_combined_group() -> None:
    combined = ", ".join(WHATSAPP_LOGIN_SELECTORS)
    assert WHATSAPP_LOGIN_SELECTOR == combined


@pytest.mark.asyncio
async def test_rnf01_login_page_finds_qr_with_primary_selector(page: Page, qr_login_html: str) -> None:
    await page.set_content(qr_login_html)
    login_page = WhatsAppLoginPage(page)
    assert await login_page.is_visible(timeout_ms=2_000)
    await login_page.wait_for_login_screen(timeout_ms=2_000)


@pytest.mark.asyncio
async def test_rnf01_login_page_fallback_when_primary_selector_removed(page: Page) -> None:
    await page.set_content(
        '<!DOCTYPE html><html><body>'
        '<div data-testid="link-device-qrcode-alt-linking-help">Precisa de ajuda?</div>'
        "</body></html>"
    )
    login_page = WhatsAppLoginPage(page)
    assert await login_page.is_visible(timeout_ms=2_000)


@pytest.mark.asyncio
async def test_rnf01_selector_group_tries_fallbacks_in_order(page: Page) -> None:
    await page.set_content(
        '<html><body><div id="pane-side" style="width:100px;height:20px">side</div></body></html>'
    )
    group = SelectorGroup("main_ui", WHATSAPP_MAIN_SELECTORS)
    locator = await group.first_visible(page, timeout_ms=2_000)
    assert locator is not None
    assert await locator.get_attribute("id") == "pane-side"


@pytest.mark.asyncio
async def test_rnf01_main_page_detects_logged_in_layout(page: Page) -> None:
    await page.set_content(
        """
        <html><body>
          <div id="pane-side"></div>
          <div contenteditable="true" role="textbox" data-tab="3">Pesquisar</div>
        </body></html>
        """
    )
    main_page = WhatsAppMainPage(page)
    assert await main_page.is_loaded(timeout_ms=2_000)
    await main_page.wait_for_main_ui(timeout_ms=2_000)


@pytest.mark.browser
@pytest.mark.asyncio
async def test_rnf01_live_whatsapp_login_selectors(async_page: Page) -> None:
    from wa_selectors import WA_URL

    await async_page.goto(WA_URL, wait_until="domcontentloaded", timeout=60_000)
    login_page = WhatsAppLoginPage(async_page)
    main_page = WhatsAppMainPage(async_page)

    if await main_page.is_loaded(timeout_ms=15_000):
        await main_page.wait_for_main_ui(timeout_ms=15_000)
    else:
        await login_page.wait_for_login_screen(timeout_ms=60_000)
