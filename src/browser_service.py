"""Serviços de navegador Playwright reutilizáveis pela API Flask e pelos testes."""

from __future__ import annotations

from playwright.async_api import BrowserContext, Page, Playwright

from wa_selectors import WHATSAPP_LOGIN_SELECTOR
from whatsapp_auto_downloader import AppConfig, open_whatsapp, wait_for_whatsapp_ready


async def initialize_browser(config: AppConfig) -> tuple[Playwright, BrowserContext, Page]:
    """Inicializa o Playwright e abre o WhatsApp Web com perfil persistente."""
    return await open_whatsapp(config)


async def wait_for_login_element(page: Page, timeout_seconds: int = 60) -> None:
    """Aguarda elementos dinâmicos da tela de login (QR Code / ajuda)."""
    await page.wait_for_selector(
        WHATSAPP_LOGIN_SELECTOR,
        timeout=timeout_seconds * 1000,
        state="visible",
    )


async def wait_for_dynamic_ready(page: Page, timeout_seconds: int) -> None:
    """Aguarda a interface principal ou tela de login ficarem prontas."""
    await wait_for_whatsapp_ready(page, timeout_seconds)
