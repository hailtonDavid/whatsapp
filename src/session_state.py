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

_DETECT_STATE_JS = """
() => {
  const bodyText = (document.body?.innerText || "").toLowerCase();
  if (!bodyText.includes("whatsapp")) {
    return "unknown";
  }

  const isVisible = (el) => {
    if (!el) return false;
    const style = window.getComputedStyle(el);
    if (style.display === "none" || style.visibility === "hidden" || style.opacity === "0") {
      return false;
    }
    const rect = el.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0;
  };

  const qr = document.querySelector(
    'canvas[aria-label*="QR" i], canvas[aria-label*="Scan" i], [data-testid="qrcode"], [data-testid="link-device-qrcode-alt-linking-help"]'
  );
  if (isVisible(qr)) {
    return "login_qr";
  }

  const hasSidePane = document.querySelector(
    "#pane-side, [data-testid='chat-list'], [data-testid='chat-list-search']"
  );
  const hasSearch = document.querySelector(
    "[aria-label*='Pesquisar' i], [aria-label*='Search' i], [aria-label*='conversa' i]"
  );
  const chatRows = document.querySelectorAll(
    "[data-testid='cell-frame-container'], #pane-side [role='listitem'], #pane-side [role='row']"
  ).length;
  const hasEditable = document.querySelectorAll(
    "div[contenteditable='true'], [role='textbox']"
  ).length;
  const hasSidePaneMarkers = document.querySelectorAll(
    "[aria-label], [role='grid'], [role='listbox']"
  ).length;

  if (
    isVisible(hasSidePane) ||
    isVisible(hasSearch) ||
    chatRows >= 1 ||
    hasEditable >= 1 ||
    hasSidePaneMarkers >= 3
  ) {
    return "logged_in";
  }

  return "unknown";
}
"""

_MAIN_INTERFACE_JS = """
() => {
  const text = (document.body?.innerText || "").toLowerCase();
  const hasWhatsapp = text.includes("whatsapp");
  const hasEditable = document.querySelectorAll(
    "div[contenteditable='true'], [role='textbox']"
  ).length > 0;
  const hasMessages = document.querySelectorAll(
    "div.copyable-text[data-pre-plain-text], div[data-pre-plain-text], div.message-in, div.message-out"
  ).length > 0;
  const hasSidePane = document.querySelectorAll(
    "[aria-label], [role='grid'], [role='listbox']"
  ).length > 0;
  return hasWhatsapp && (hasEditable || hasMessages || hasSidePane);
}
"""


async def whatsapp_main_interface_loaded(page: Page) -> bool:
    try:
        return bool(await page.evaluate(_MAIN_INTERFACE_JS))
    except Exception:
        return False


async def resolve_session_state(
    page: Page,
    *,
    timeout_ms: int = 30_000,
    ready_timeout_seconds: int | None = None,
) -> SessionState:
    """Aguarda interface e classifica sessão — evita 'unknown' por carregamento lento."""
    if ready_timeout_seconds is not None and ready_timeout_seconds > 0:
        from whatsapp_auto_downloader import wait_for_whatsapp_ready

        capped = min(int(ready_timeout_seconds), 90)
        await wait_for_whatsapp_ready(page, capped)

    state = await wait_for_stable_session_state(page, timeout_ms=timeout_ms)
    if state != "unknown":
        return state

    if await whatsapp_main_interface_loaded(page):
        return "logged_in"

    return await detect_whatsapp_session_state(page, timeout_ms=10_000)


async def _detect_state_via_js(page: Page) -> SessionState | None:
    try:
        raw = await page.evaluate(_DETECT_STATE_JS)
    except Exception:
        return None
    if raw in ("logged_in", "login_qr", "unknown"):
        return raw  # type: ignore[return-value]
    return None


async def detect_whatsapp_session_state(page: Page, *, timeout_ms: int = 8_000) -> SessionState:
    """Classifica a tela atual: logado, QR/login ou indeterminado."""
    try:
        if page.is_closed():
            return "unknown"
    except Exception:
        return "unknown"

    js_state = await _detect_state_via_js(page)
    if js_state in ("logged_in", "login_qr"):
        return js_state

    per_selector_timeout = max(1_500, timeout_ms // max(len(WHATSAPP_MAIN_SELECTORS), 1))

    if await MAIN_GROUP.any_visible(page, timeout_ms=per_selector_timeout):
        return "logged_in"

    if await LOGIN_GROUP.any_visible(page, timeout_ms=per_selector_timeout):
        return "login_qr"

    return js_state or "unknown"


async def wait_for_stable_session_state(
    page: Page,
    *,
    timeout_ms: int = 30_000,
) -> SessionState:
    """Aguarda sair de 'unknown' dentro do tempo limite."""
    try:
        if page.is_closed():
            return "unknown"
    except Exception:
        return "unknown"

    loop = asyncio.get_event_loop()
    deadline = loop.time() + (timeout_ms / 1000)
    state: SessionState = "unknown"
    while loop.time() < deadline:
        try:
            if page.is_closed():
                return "unknown"
        except Exception:
            return "unknown"
        state = await detect_whatsapp_session_state(page, timeout_ms=3_000)
        if state != "unknown":
            return state
        await asyncio.sleep(0.5)
    return state
