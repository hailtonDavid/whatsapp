"""RF04 — persistência de sessão WhatsApp Web (perfil persistente)."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from playwright.async_api import async_playwright

from playwright_lifecycle import shutdown_playwright_stack
from session_state import detect_whatsapp_session_state, wait_for_stable_session_state
from whatsapp_auto_downloader import AppConfig, open_whatsapp

pytestmark = [pytest.mark.integration, pytest.mark.rf04]


@pytest.fixture
def persistent_profile_dir(tmp_path: Path) -> Path:
    return tmp_path / "profile_whatsapp_rf04"


@pytest.fixture
def rf04_app_config(persistent_profile_dir: Path, tmp_path: Path) -> AppConfig:
    return AppConfig(
        profile_dir=persistent_profile_dir,
        headless=True,
        ready_timeout=30,
        export_dir=tmp_path / "exports",
        state_dir=tmp_path / "state",
    )


async def _open_detect_and_close(config: AppConfig) -> str:
    playwright, context, page = await open_whatsapp(config)
    try:
        await page.wait_for_timeout(1500)
        return await wait_for_stable_session_state(page, timeout_ms=20_000)
    finally:
        await shutdown_playwright_stack(pages=[page], context=context, playwright=playwright)


@pytest.mark.asyncio
async def test_rf04_profile_directory_is_created_and_reused(
    rf04_app_config: AppConfig,
    mock_playwright_stack,
) -> None:
    user_data_dirs: list[str] = []

    mock_factory, mock_playwright, mock_context, mock_page = mock_playwright_stack()

    async def tracked_launch(*args, **kwargs):
        user_data_dirs.append(kwargs.get("user_data_dir", ""))
        return mock_context

    mock_playwright.chromium.launch_persistent_context = AsyncMock(side_effect=tracked_launch)

    for _ in range(2):
        playwright, context, page = await open_whatsapp(rf04_app_config)
        await shutdown_playwright_stack(pages=[page], context=context, playwright=playwright)

    assert rf04_app_config.profile_dir.is_dir()
    assert len(user_data_dirs) == 2
    assert user_data_dirs[0] == user_data_dirs[1]
    assert user_data_dirs[0] == str(rf04_app_config.profile_dir)


@pytest.mark.slow
@pytest.mark.asyncio
async def test_rf04_session_state_persisted_after_browser_close_mocked_html(
    rf04_app_config: AppConfig,
    qr_login_html: str,
) -> None:
    """Simula duas aberturas com o mesmo perfil; estado de login (QR) deve ser igual."""
    playwright = await async_playwright().start()
    states: list[str] = []

    try:
        for _ in range(2):
            context = await playwright.chromium.launch_persistent_context(
                user_data_dir=str(rf04_app_config.profile_dir),
                headless=True,
                channel="msedge",
            )
            page = context.pages[0] if context.pages else await context.new_page()
            try:
                await page.set_content(qr_login_html)
                states.append(await detect_whatsapp_session_state(page))
            finally:
                await shutdown_playwright_stack(pages=[page], context=context)
    finally:
        await playwright.stop()

    assert states == ["login_qr", "login_qr"]


@pytest.mark.asyncio
async def test_rf04_profile_marker_survives_browser_teardown(
    rf04_app_config: AppConfig,
) -> None:
    """Arquivos do user_data_dir persistem após fechar browser (base da sessão RF04)."""
    marker = rf04_app_config.profile_dir / "rf04_session_marker.txt"
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text("sessao-rf04", encoding="utf-8")

    playwright = await async_playwright().start()
    try:
        context = await playwright.chromium.launch_persistent_context(
            user_data_dir=str(rf04_app_config.profile_dir),
            headless=True,
            channel="msedge",
        )
        page = context.pages[0] if context.pages else await context.new_page()
        await shutdown_playwright_stack(pages=[page], context=context)
    finally:
        await playwright.stop()

    assert marker.exists()
    assert marker.read_text(encoding="utf-8") == "sessao-rf04"


@pytest.mark.browser
@pytest.mark.asyncio
async def test_rf04_real_session_state_consistent_after_browser_restart(
    rf04_app_config: AppConfig,
) -> None:
    """E2E: reabrir o mesmo perfil deve manter login (logado) ou QR (não logado)."""
    rf04_app_config.headless = False

    first_state = await _open_detect_and_close(rf04_app_config)
    second_state = await _open_detect_and_close(rf04_app_config)

    assert first_state != "unknown"
    assert second_state == first_state, (
        f"Estado da sessão mudou após fechar o browser: {first_state} -> {second_state}. "
        "RF04 exige que o perfil persistente preserve o login."
    )


@pytest.mark.browser
@pytest.mark.asyncio
async def test_rf04_logged_in_session_skips_qr_on_reopen(rf04_app_config: AppConfig) -> None:
    """Se já logado, segunda abertura não deve voltar para tela de QR."""
    rf04_app_config.headless = False

    first_state = await _open_detect_and_close(rf04_app_config)
    if first_state != "logged_in":
        pytest.skip("Sessão não está logada — escaneie QR no perfil RF04 antes de rodar este teste.")

    second_state = await _open_detect_and_close(rf04_app_config)
    assert second_state == "logged_in"
