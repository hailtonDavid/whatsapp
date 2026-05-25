"""Orquestração da automação WhatsApp Web (env → browser → login)."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv
from playwright.async_api import BrowserContext, Page, Playwright

from browser_service import initialize_browser, wait_for_login_element
from whatsapp_auto_downloader import AppConfig, load_app_config


@dataclass
class AutomationBootstrap:
    env_file: Path
    config: AppConfig
    env_loaded: bool


@dataclass
class AutomationSession:
    bootstrap: AutomationBootstrap
    playwright: Playwright
    context: BrowserContext
    page: Page


def load_env_before_browser(env_file: Path) -> AutomationBootstrap:
    """RF01: carrega .env e valida variáveis antes de qualquer inicialização do browser."""
    load_dotenv(env_file, override=True)

    profile_dir = os.getenv("WA_PROFILE_DIR")
    if not profile_dir:
        raise RuntimeError("WA_PROFILE_DIR não definido — carregue o .env antes do browser.")

    config = load_app_config(env_file=env_file)
    return AutomationBootstrap(
        env_file=env_file,
        config=config,
        env_loaded=True,
    )


async def start_automation(env_file: Path) -> AutomationSession:
    """RF05: fluxo completo — .env primeiro, depois Playwright."""
    bootstrap = load_env_before_browser(env_file)
    playwright, context, page = await initialize_browser(bootstrap.config)
    return AutomationSession(
        bootstrap=bootstrap,
        playwright=playwright,
        context=context,
        page=page,
    )


async def detect_qr_code_login(page: Page, timeout_seconds: int = 60) -> bool:
    """RF06: identifica seletor de QR Code / tela de login."""
    await wait_for_login_element(page, timeout_seconds=timeout_seconds)
    return True
