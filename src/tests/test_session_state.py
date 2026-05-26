"""Testes de resolve_session_state com interface carregada."""

from __future__ import annotations

import pytest

from session_state import (
    detect_whatsapp_session_state,
    resolve_session_state,
    whatsapp_main_interface_loaded,
)


LOGGED_IN_HTML = """
<!DOCTYPE html><html><body>
<div id="pane-side">
  <div data-testid="chat-list-search" aria-label="Pesquisar ou começar uma nova conversa"></div>
  <div data-testid="cell-frame-container">Grupo teste</div>
</div>
<div>WhatsApp Business Web</div>
</body></html>
"""


QR_HTML = """
<!DOCTYPE html><html><body>
<canvas aria-label="Scan this QR code to link a device!" role="img"></canvas>
<div>WhatsApp Web</div>
</body></html>
"""


@pytest.mark.asyncio
async def test_detect_logged_in_whatsapp_business(async_page) -> None:
    await async_page.set_content(LOGGED_IN_HTML)
    state = await detect_whatsapp_session_state(async_page, timeout_ms=2_000)
    assert state == "logged_in"


@pytest.mark.asyncio
async def test_detect_login_qr(async_page) -> None:
    await async_page.set_content(QR_HTML)
    state = await detect_whatsapp_session_state(async_page, timeout_ms=2_000)
    assert state == "login_qr"


@pytest.mark.asyncio
async def test_whatsapp_main_interface_loaded(async_page) -> None:
    await async_page.set_content(LOGGED_IN_HTML)
    assert await whatsapp_main_interface_loaded(async_page) is True


@pytest.mark.asyncio
async def test_resolve_session_state_after_interface_ready(async_page) -> None:
    await async_page.set_content(LOGGED_IN_HTML)
    state = await resolve_session_state(async_page, timeout_ms=2_000, ready_timeout_seconds=None)
    assert state == "logged_in"
