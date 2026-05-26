"""RF03 / RF06 — automação Playwright: URL WhatsApp Web e seletor de login."""

from __future__ import annotations

import pytest
from playwright.async_api import Page

from automation_service import detect_qr_code_login
from browser_service import wait_for_login_element
from pages.whatsapp_login_page import WhatsAppLoginPage
from wa_selectors import WA_URL, WHATSAPP_LOGIN_SELECTOR

pytestmark = [pytest.mark.integration]


@pytest.mark.rf03
@pytest.mark.asyncio
async def test_rf03_page_navigates_to_whatsapp_url(page: Page, qr_login_html: str) -> None:
    """RF03: Playwright navega até a URL oficial do WhatsApp Web."""

    async def fulfill_whatsapp(route):
        await route.fulfill(body=qr_login_html, content_type="text/html", status=200)

    await page.route(f"{WA_URL.rstrip('/')}/*", fulfill_whatsapp)
    await page.route(WA_URL, fulfill_whatsapp)

    response = await page.goto(WA_URL, wait_until="domcontentloaded", timeout=30_000)

    assert response is not None
    assert response.ok
    assert WA_URL.rstrip("/") in page.url.rstrip("/")


@pytest.mark.rf03
@pytest.mark.rf06
@pytest.mark.asyncio
async def test_rf03_rf06_mock_page_loads_and_waits_for_login_selector(
    page: Page,
    qr_login_html: str,
) -> None:
    """RF03+RF06 offline: conteúdo de login carregado e seletor QR detectado."""
    await page.set_content(qr_login_html, wait_until="domcontentloaded")

    assert "whatsapp.com" in WA_URL
    detected = await detect_qr_code_login(page, timeout_seconds=5)
    login_page = WhatsAppLoginPage(page)

    assert detected is True
    assert await login_page.is_visible(timeout_ms=3_000)
    assert await page.locator(WHATSAPP_LOGIN_SELECTOR).first.is_visible()


@pytest.mark.rf06
@pytest.mark.asyncio
async def test_rf06_wait_for_login_element_on_mock_page(page: Page, qr_login_html: str) -> None:
    """RF06: wait_for_login_element encontra canvas/fallback na tela de QR."""
    await page.set_content(qr_login_html, wait_until="domcontentloaded")

    await wait_for_login_element(page, timeout_seconds=5)

    assert await page.locator(WHATSAPP_LOGIN_SELECTOR).first.is_visible()


@pytest.mark.rf03
@pytest.mark.rf06
@pytest.mark.browser
@pytest.mark.asyncio
async def test_rf03_rf06_live_whatsapp_web_login_selector(page: Page) -> None:
    """RF03+RF06 E2E: URL real carregada e seletor de login visível."""
    await page.goto(WA_URL, wait_until="domcontentloaded", timeout=60_000)

    assert WA_URL.rstrip("/") in page.url.rstrip("/")

    detected = await detect_qr_code_login(page, timeout_seconds=60)

    assert detected is True
    assert await page.locator(WHATSAPP_LOGIN_SELECTOR).first.is_visible()


@pytest.mark.rf03
@pytest.mark.browser
@pytest.mark.asyncio
async def test_rf03_live_page_title_indicates_whatsapp(page: Page) -> None:
    """RF03: página live retorna título coerente com WhatsApp Web."""
    await page.goto(WA_URL, wait_until="domcontentloaded", timeout=60_000)

    title = await page.title()
    assert "whatsapp" in title.lower()


def test_message_state_database_reset_before_each_test(message_state_db) -> None:
    """Confirma que o banco message_state.json inicia vazio após reset autouse."""
    assert message_state_db.data == {"targets": {}}
    assert message_state_db.seen_hashes("grupo_teste") == set()


def test_message_state_database_isolated_from_previous_test(message_state_db) -> None:
    """Segundo teste: hashes gravados no anterior não devem vazar."""
    assert message_state_db.seen_hashes("grupo_teste") == set()


def test_default_browser_channel_prefers_msedge_on_windows(monkeypatch: pytest.MonkeyPatch) -> None:
    from whatsapp_auto_downloader import default_browser_channel

    monkeypatch.delenv("WA_BROWSER_CHANNEL", raising=False)
    monkeypatch.setattr("whatsapp_auto_downloader.platform.system", lambda: "Windows")

    assert default_browser_channel() == "msedge"


def test_default_browser_channel_none_when_bundled(monkeypatch: pytest.MonkeyPatch) -> None:
    from whatsapp_auto_downloader import default_browser_channel

    monkeypatch.setenv("WA_BROWSER_CHANNEL", "bundled")

    assert default_browser_channel() is None


def test_name_search_queries_supports_partial_group_name() -> None:
    from whatsapp_auto_downloader import _name_search_queries

    queries = _name_search_queries("Tal pai, tal filhas")

    assert queries[0] == "Tal pai, tal filhas"
    assert "Tal pai" in queries


@pytest.mark.asyncio
async def test_find_chat_result_prefers_conversas_title(page: Page) -> None:
    from whatsapp_auto_downloader import FIND_CHAT_RESULT_JS

    await page.set_content(
        """
        <html><body>
          <div id="pane-side">
            <div>Mensagens</div>
            <div role="listitem"><span>Curso IA FDTE</span></div>
            <div>Conversas</div>
            <div role="listitem"><span title="tal pai, tal filhas">tal pai, tal filhas</span></div>
          </div>
        </body></html>
        """
    )
    result = await page.evaluate(
        FIND_CHAT_RESULT_JS,
        {"query": "tal pai", "fullName": "tal pai, tal filhas"},
    )

    assert result is not None
    assert "tal pai" in (result.get("title") or result.get("text") or "")


@pytest.mark.asyncio
async def test_chat_send_state_detects_unsupported_browser(page: Page) -> None:
    from whatsapp_auto_downloader import CHAT_SEND_STATE_JS

    await page.set_content(
        "<html><body>O WhatsApp funciona no Google Chrome 85 ou posterior.</body></html>"
    )
    state = await page.evaluate(CHAT_SEND_STATE_JS)

    assert state["unsupported_browser"] is True
    assert state["ok"] is False


@pytest.mark.asyncio
async def test_wait_for_chat_send_ready_rejects_unsupported_browser(page: Page) -> None:
    from whatsapp_auto_downloader import wait_for_chat_send_ready

    await page.set_content(
        "<html><body>O WhatsApp funciona no Google Chrome 85 ou posterior.</body></html>"
    )
    result = await wait_for_chat_send_ready(page, timeout_ms=1500)

    assert result["ok"] is False
    assert "rejeitou o navegador" in str(result.get("error", "")).lower()
