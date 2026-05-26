"""Serviços de navegador Playwright reutilizáveis pela API Flask e pelos testes."""

from __future__ import annotations

from pathlib import Path

from playwright.async_api import BrowserContext, Page, Playwright

from browser_diagnostics import wait_for_visible_selector
from wa_selectors import WHATSAPP_LOGIN_SELECTOR
from whatsapp_auto_downloader import AppConfig, open_whatsapp, wait_for_whatsapp_ready


async def initialize_browser(config: AppConfig) -> tuple[Playwright, BrowserContext, Page]:
    """Inicializa o Playwright e abre o WhatsApp Web com perfil persistente."""
    return await open_whatsapp(config)


async def wait_for_login_element(
    page: Page,
    timeout_seconds: int = 60,
    *,
    diagnostics_dir: Path | None = None,
) -> None:
    """Aguarda elementos dinâmicos da tela de login (QR Code / ajuda) — RF06."""
    await wait_for_visible_selector(
        page,
        WHATSAPP_LOGIN_SELECTOR,
        timeout_seconds=timeout_seconds,
        label="rf06_login",
        diagnostics_dir=diagnostics_dir,
        state="visible",
    )


async def wait_for_dynamic_ready(page: Page, timeout_seconds: int) -> None:
    """Aguarda a interface principal ou tela de login ficarem prontas."""
    await wait_for_whatsapp_ready(page, timeout_seconds)
