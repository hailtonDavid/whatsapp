"""Page Object — tela de login / QR Code (RF06)."""

from __future__ import annotations

from pages.base_page import BasePage, SelectorGroup
from wa_selectors import WHATSAPP_LOGIN_SELECTORS


class WhatsAppLoginPage(BasePage):
    login_selectors = SelectorGroup("login_qr", WHATSAPP_LOGIN_SELECTORS)

    async def is_visible(self, *, timeout_ms: int = 5_000) -> bool:
        return await self.login_selectors.any_visible(self.page, timeout_ms=timeout_ms)

    async def wait_for_login_screen(self, *, timeout_ms: int = 60_000) -> None:
        locator = await self.login_selectors.first_visible(self.page, timeout_ms=timeout_ms)
        if locator is None:
            tried = ", ".join(WHATSAPP_LOGIN_SELECTORS)
            raise AssertionError(f"Tela de login não encontrada. Seletores tentados: {tried}")

    async def qr_canvas(self, *, timeout_ms: int = 10_000):
        group = SelectorGroup(
            "qr_canvas",
            (
                'canvas[aria-label*="QR" i]',
                'canvas[aria-label*="Scan" i]',
            ),
        )
        locator = await group.first_visible(self.page, timeout_ms=timeout_ms)
        if locator is None:
            raise AssertionError("Canvas de QR Code não encontrado.")
        return locator
