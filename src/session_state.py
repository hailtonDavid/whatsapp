"""Detecção de estado da sessão WhatsApp Web (RF04)."""

from __future__ import annotations

import asyncio
from typing import Literal

from playwright.async_api import Page

from pages.base_page import SelectorGroup
from wa_selectors import WHATSAPP_LOGIN_SELECTORS, WHATSAPP_MAIN_SELECTORS

SessionState = Literal["logged_in", "login_qr", "unknown"]

LOGIN_GROUP = SelectorGroup("login_qr", WHATSAPP_LOGIN_SELECTORS)
MAIN_GROUP = SelectorGroup("main_ui", WHATSAPP_MAIN_SELECTORS)


async def detect_whatsapp_session_state(page: Page, *, timeout_ms: int = 8_000) -> SessionState:
    """Classifica a tela atual: logado, QR/login ou indeterminado."""
    per_selector_timeout = max(1_500, timeout_ms // max(len(WHATSAPP_MAIN_SELECTORS), 1))

    if await MAIN_GROUP.any_visible(page, timeout_ms=per_selector_timeout):
        return "logged_in"

    if await LOGIN_GROUP.any_visible(page, timeout_ms=per_selector_timeout):
        return "login_qr"

    return "unknown"


async def wait_for_stable_session_state(
    page: Page,
    *,
    timeout_ms: int = 30_000,
) -> SessionState:
    """Aguarda sair de 'unknown' dentro do tempo limite."""
    loop = asyncio.get_event_loop()
    deadline = loop.time() + (timeout_ms / 1000)
    state: SessionState = "unknown"
    while loop.time() < deadline:
        state = await detect_whatsapp_session_state(page, timeout_ms=3_000)
        if state != "unknown":
            return state
        await asyncio.sleep(0.5)
    return state
