"""Page Object — interface principal após login."""

from __future__ import annotations

from pages.base_page import BasePage, SelectorGroup
from wa_selectors import (
    WHATSAPP_MAIN_SELECTORS,
    WHATSAPP_MESSAGE_BOX_SELECTORS,
    WHATSAPP_SEARCH_SELECTORS,
)


class WhatsAppMainPage(BasePage):
    main_selectors = SelectorGroup("main_ui", WHATSAPP_MAIN_SELECTORS)
    search_selectors = SelectorGroup("search_box", WHATSAPP_SEARCH_SELECTORS)
    message_box_selectors = SelectorGroup("message_box", WHATSAPP_MESSAGE_BOX_SELECTORS)

    async def is_loaded(self, *, timeout_ms: int = 8_000) -> bool:
        return await self.main_selectors.any_visible(self.page, timeout_ms=timeout_ms)

    async def wait_for_main_ui(self, *, timeout_ms: int = 60_000) -> None:
        locator = await self.main_selectors.first_visible(self.page, timeout_ms=timeout_ms)
        if locator is None:
            tried = ", ".join(WHATSAPP_MAIN_SELECTORS)
            raise AssertionError(f"Interface principal não encontrada. Seletores: {tried}")

    async def search_box(self, *, timeout_ms: int = 10_000):
        locator = await self.search_selectors.first_visible(self.page, timeout_ms=timeout_ms)
        if locator is None:
            raise AssertionError("Caixa de pesquisa não encontrada.")
        return locator
