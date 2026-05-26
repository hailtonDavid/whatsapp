"""Fixtures compartilhadas para testes da aplicação Flask e Playwright."""

from __future__ import annotations

import gc
import json
import os
import sys
from collections.abc import AsyncGenerator, Generator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from playwright.async_api import Browser, BrowserContext, Page, Playwright, async_playwright

from app import create_app
from automation_service import reset_held_automation_session_for_tests
from playwright_lifecycle import shutdown_playwright_stack
from whatsapp_auto_downloader import MessageState, load_app_config

MESSAGE_STATE_FILENAME = "message_state.json"
EXPORT_ARTIFACTS = (
    "send/last_send.json",
    "send/sent_log.jsonl",
    "groups/groups.json",
    "groups/groups_targets_template.json",
    "contacts/contacts.json",
)


def reset_message_state_database(state_dir: Path) -> None:
    """Reinicia o banco de deduplicação (message_state.json) para estado vazio."""
    state_dir.mkdir(parents=True, exist_ok=True)
    db_path = state_dir / MESSAGE_STATE_FILENAME
    db_path.write_text(json.dumps({"targets": {}}, ensure_ascii=False), encoding="utf-8")


def reset_export_artifacts(export_dir: Path) -> None:
    """Remove artefatos de export no sandbox do teste."""
    for rel in EXPORT_ARTIFACTS:
        path = export_dir / rel
        if path.is_file():
            path.unlink()


def reset_persistent_test_state(state_dir: Path, export_dir: Path) -> None:
    """Reset completo de estado persistente antes de cada teste."""
    reset_held_automation_session_for_tests()
    reset_message_state_database(state_dir)
    reset_export_artifacts(export_dir)


@pytest.fixture(scope="session", autouse=True)
def _windows_playwright_session_cleanup() -> Generator[None, None, None]:
    """Coleta lixo residual de subprocessos Playwright após a suíte no Windows."""
    yield
    if sys.platform == "win32":
        gc.collect()
        import time

        time.sleep(0.15)


def _browser_launch_kwargs() -> dict[str, Any]:
    channel = os.getenv("WA_BROWSER_CHANNEL", "msedge").strip()
    kwargs: dict[str, Any] = {"headless": True}
    if channel.lower() not in {"", "none", "bundled", "chromium"}:
        kwargs["channel"] = channel
    return kwargs


@pytest.fixture
def test_sandbox(tmp_path: Path) -> Path:
    """Sandbox isolado por teste: .env, state DB e exports."""
    sandbox = tmp_path / "sandbox"
    (sandbox / "state").mkdir(parents=True, exist_ok=True)
    (sandbox / "exports" / "groups").mkdir(parents=True, exist_ok=True)
    (sandbox / "exports" / "contacts").mkdir(parents=True, exist_ok=True)
    (sandbox / "exports" / "send").mkdir(parents=True, exist_ok=True)
    return sandbox


@pytest.fixture(autouse=True)
def reset_test_state_before_each_test(test_sandbox: Path) -> Generator[None, None, None]:
    """Reseta banco de estado e sessão Playwright antes (e após) cada teste."""
    state_dir = test_sandbox / "state"
    export_dir = test_sandbox / "exports"
    reset_persistent_test_state(state_dir, export_dir)
    yield
    reset_persistent_test_state(state_dir, export_dir)


@pytest.fixture(autouse=True)
def reset_wa_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in (
        "WA_PROFILE_DIR",
        "WA_HEADLESS",
        "WA_READY_TIMEOUT",
        "WA_EXPORT_DIR",
        "WA_STATE_DIR",
    ):
        monkeypatch.delenv(key, raising=False)


@pytest.fixture
def env_file(test_sandbox: Path) -> Path:
    path = test_sandbox / ".env"
    path.write_text(
        "\n".join(
            [
                "WA_PROFILE_DIR=profile_whatsapp_test",
                "WA_HEADLESS=true",
                "WA_READY_TIMEOUT=30",
                f"WA_EXPORT_DIR={(test_sandbox / 'exports').as_posix()}",
                f"WA_STATE_DIR={(test_sandbox / 'state').as_posix()}",
            ]
        ),
        encoding="utf-8",
    )
    return path


@pytest.fixture
def message_state_db(env_file: Path, test_sandbox: Path) -> MessageState:
    """Instância do banco de estado já resetada pelo autouse fixture."""
    return MessageState(test_sandbox / "state" / MESSAGE_STATE_FILENAME)


@pytest.fixture
def diagnostics_dir(test_sandbox: Path) -> Path:
    path = test_sandbox / "diagnostics"
    path.mkdir(parents=True, exist_ok=True)
    return path


@pytest.fixture
def isolated_export_paths(test_sandbox: Path) -> dict[str, str]:
    """Paths de export/grupos isolados — não leem exports/ de produção."""
    groups_dir = test_sandbox / "exports" / "groups"
    contacts_dir = test_sandbox / "exports" / "contacts"
    groups_dir.mkdir(parents=True, exist_ok=True)
    contacts_dir.mkdir(parents=True, exist_ok=True)
    return {
        "default_groups_output": str(groups_dir / "groups.json"),
        "default_groups_targets": str(groups_dir / "groups_targets_template.json"),
        "default_contacts_output": str(contacts_dir / "contacts.json"),
    }


@pytest.fixture
def app(env_file: Path, isolated_export_paths: dict[str, str]):
    return create_app(
        env_file=env_file,
        default_groups_output=isolated_export_paths["default_groups_output"],
        default_groups_targets=isolated_export_paths["default_groups_targets"],
        default_contacts_output=isolated_export_paths["default_contacts_output"],
    )


@pytest.fixture
def client(app):
    return app.test_client()


@pytest.fixture
def targets_file(test_sandbox: Path) -> Path:
    path = test_sandbox / "targets.json"
    path.write_text(
        json.dumps(
            {
                "targets": [
                    {
                        "id": "numero_teste",
                        "type": "phone",
                        "phone": "5562999000000",
                        "enabled": True,
                        "send": {
                            "enabled": True,
                            "message": "Mensagem via Flask",
                        },
                    },
                    {
                        "id": "grupo_teste",
                        "type": "group",
                        "name": "Grupo Teste",
                        "enabled": True,
                        "send": {
                            "enabled": True,
                            "message": "Mensagem via Flask",
                        },
                    }
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return path


@dataclass
class PlaywrightResources:
    """Recursos Playwright com fechamento idempotente e ordenado."""

    playwright: Playwright
    browser: Browser | None = None
    context: BrowserContext | None = None
    pages: list[Page] = field(default_factory=list)
    closed: bool = False

    async def close_all(self) -> None:
        if self.closed:
            return
        await shutdown_playwright_stack(
            pages=list(self.pages),
            context=self.context,
            browser=self.browser,
            playwright=self.playwright,
        )
        self.pages.clear()
        self.context = None
        self.browser = None
        self.closed = True


@pytest.fixture
async def playwright_stack() -> AsyncGenerator[PlaywrightResources, None]:
    """Fixture base: único ponto de teardown pages → context → browser → playwright."""
    resources = PlaywrightResources(playwright=await async_playwright().start())
    try:
        yield resources
    finally:
        await resources.close_all()


@pytest.fixture
async def playwright_engine(playwright_stack: PlaywrightResources) -> AsyncGenerator[Playwright, None]:
    """Expõe Playwright sem teardown próprio — delegado ao playwright_stack."""
    yield playwright_stack.playwright


@pytest.fixture
async def browser(playwright_stack: PlaywrightResources) -> AsyncGenerator[Browser, None]:
    """Browser efêmero; fechamento centralizado no playwright_stack."""
    launched = await playwright_stack.playwright.chromium.launch(**_browser_launch_kwargs())
    playwright_stack.browser = launched
    yield launched


@pytest.fixture
async def async_page(browser: Browser, playwright_stack: PlaywrightResources) -> AsyncGenerator[Page, None]:
    """Página Playwright; fechamento centralizado no playwright_stack."""
    page = await browser.new_page()
    playwright_stack.pages.append(page)
    yield page


@pytest.fixture
async def page(async_page: Page) -> AsyncGenerator[Page, None]:
    """Fixture Playwright padrão para automação (RF03 / RF06)."""
    yield async_page


@pytest.fixture
async def managed_playwright_resources(
    playwright_stack: PlaywrightResources,
) -> AsyncGenerator[PlaywrightResources, None]:
    """Alias explícito para testes que montam contexto manualmente."""
    yield playwright_stack


@pytest.fixture
async def tracked_browser(playwright_stack: PlaywrightResources) -> AsyncGenerator[dict[str, Any], None]:
    """Browser rastreado; close() registrado antes do teardown do stack."""
    launched = await playwright_stack.playwright.chromium.launch(**_browser_launch_kwargs())
    playwright_stack.browser = launched
    state = {"browser": launched, "close_calls": 0, "stack": playwright_stack}

    original_close = launched.close

    async def tracked_close(*args, **kwargs):
        state["close_calls"] += 1
        playwright_stack.browser = None
        return await original_close(*args, **kwargs)

    launched.close = tracked_close  # type: ignore[method-assign]
    yield state


@pytest.fixture
def mock_playwright_stack(monkeypatch: pytest.MonkeyPatch):
    """Stack Playwright mockado para testes de integração offline."""

    def _factory(*, pages: list | None = None):
        mock_page = AsyncMock()
        mock_page.is_closed = MagicMock(return_value=False)
        mock_context = MagicMock()
        mock_context.pages = pages if pages is not None else [mock_page]
        mock_context.new_page = AsyncMock(return_value=mock_page)
        mock_context.close = AsyncMock()

        mock_playwright = AsyncMock()
        mock_playwright.chromium.launch_persistent_context = AsyncMock(return_value=mock_context)
        mock_playwright.stop = AsyncMock()

        mock_factory = MagicMock()
        mock_factory.start = AsyncMock(return_value=mock_playwright)
        monkeypatch.setattr("whatsapp_auto_downloader.async_playwright", lambda: mock_factory)

        return mock_factory, mock_playwright, mock_context, mock_page

    return _factory


@pytest.fixture
def mock_playwright_stack_with_cleanup_tracking(monkeypatch: pytest.MonkeyPatch):
    """Mock Playwright que registra chamadas de close/stop para auditoria de ciclo de vida."""

    def _factory(*, pages: list | None = None):
        cleanup = {"context_closed": 0, "playwright_stopped": 0}

        mock_page = AsyncMock()
        mock_page.is_closed = MagicMock(return_value=False)
        mock_context = MagicMock()
        mock_context.pages = pages if pages is not None else [mock_page]
        mock_context.new_page = AsyncMock(return_value=mock_page)

        async def close_context():
            cleanup["context_closed"] += 1

        mock_context.close = AsyncMock(side_effect=close_context)

        mock_playwright = AsyncMock()
        mock_playwright.chromium.launch_persistent_context = AsyncMock(return_value=mock_context)

        async def stop_playwright():
            cleanup["playwright_stopped"] += 1

        mock_playwright.stop = AsyncMock(side_effect=stop_playwright)

        mock_factory = MagicMock()
        mock_factory.start = AsyncMock(return_value=mock_playwright)
        monkeypatch.setattr("whatsapp_auto_downloader.async_playwright", lambda: mock_factory)

        return mock_factory, mock_playwright, mock_context, mock_page, cleanup

    return _factory


@pytest.fixture
def qr_login_html() -> str:
    return (
        '<!DOCTYPE html><html><body>'
        '<canvas aria-label="Scan this QR code to link a device!" role="img"></canvas>'
        '<div data-testid="link-device-qrcode-alt-linking-help">Precisa de ajuda?</div>'
        "</body></html>"
    )


@pytest.fixture
def app_config(env_file: Path):
    return load_app_config(env_file=env_file)
