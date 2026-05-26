"""Captura de diagnóstico quando elementos dinâmicos não são encontrados (RF06)."""

from __future__ import annotations

import logging
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from playwright.async_api import Page
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DIAGNOSTICS_DIR = PROJECT_ROOT / "exports" / "diagnostics"


class DynamicElementNotFoundError(Exception):
    """Elemento dinâmico não encontrado dentro do tempo limite."""

    def __init__(
        self,
        selector: str,
        label: str,
        *,
        timeout_seconds: int,
        diagnostics: dict[str, Any],
    ) -> None:
        self.selector = selector
        self.label = label
        self.timeout_seconds = timeout_seconds
        self.diagnostics = diagnostics
        summary = diagnostics.get("summary", "sem diagnóstico")
        super().__init__(
            f"{label}: seletor não encontrado em {timeout_seconds}s — {selector}. {summary}"
        )


def _safe_label(label: str) -> str:
    value = re.sub(r"[^a-z0-9_\-]+", "_", label.strip().lower())
    return re.sub(r"_+", "_", value).strip("_") or "dynamic_element"


def _build_summary(diagnostics: dict[str, Any]) -> str:
    dom = diagnostics.get("dom") or {}
    parts = [
        f"url={diagnostics.get('url', '?')}",
        f"title={diagnostics.get('title', '?')}",
    ]
    if dom.get("selector_matches") is not None:
        parts.append(f"selector_matches={dom['selector_matches']}")
    if diagnostics.get("screenshot"):
        parts.append(f"screenshot={diagnostics['screenshot']}")
    if diagnostics.get("html_dump"):
        parts.append(f"html={diagnostics['html_dump']}")
    return "; ".join(parts)


async def capture_dom_state(page: Page, *, selector: str | None = None) -> dict[str, Any]:
    script = """
    (selector) => {
        const bodyText = (document.body?.innerText || "").replace(/\\s+/g, " ").trim();
        const result = {
            body_text_preview: bodyText.slice(0, 500),
            body_text_length: bodyText.length,
            contenteditable_count: document.querySelectorAll("div[contenteditable='true']").length,
            textbox_count: document.querySelectorAll("[role='textbox']").length,
            canvas_count: document.querySelectorAll("canvas").length,
            qr_help_count: document.querySelectorAll(
                '[data-testid="link-device-qrcode-alt-linking-help"]'
            ).length,
        };
        if (selector) {
            try {
                result.selector_matches = document.querySelectorAll(selector).length;
            } catch (err) {
                result.selector_error = String(err);
            }
        }
        return result;
    }
    """
    state = await page.evaluate(script, selector)
    return state if isinstance(state, dict) else {"raw": state}


async def capture_page_diagnostics(
    page: Page,
    *,
    label: str,
    selector: str | None = None,
    output_dir: Path | None = None,
) -> dict[str, Any]:
    target_dir = output_dir or DEFAULT_DIAGNOSTICS_DIR
    target_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    safe = _safe_label(label)

    diagnostics: dict[str, Any] = {
        "label": label,
        "selector": selector,
        "url": page.url,
        "captured_at": datetime.now().isoformat(timespec="seconds"),
    }

    try:
        diagnostics["title"] = await page.title()
    except Exception as exc:
        diagnostics["title_error"] = str(exc)

    try:
        diagnostics["dom"] = await capture_dom_state(page, selector=selector)
    except Exception as exc:
        diagnostics["dom_error"] = str(exc)

    screenshot_path = target_dir / f"{safe}_{stamp}.png"
    try:
        await page.screenshot(path=str(screenshot_path), full_page=True)
        diagnostics["screenshot"] = str(screenshot_path)
        logger.warning("Screenshot de diagnóstico salvo: %s", screenshot_path)
    except Exception as exc:
        diagnostics["screenshot_error"] = str(exc)
        logger.warning("Falha ao salvar screenshot (%s): %s", label, exc)

    html_path = target_dir / f"{safe}_{stamp}.html"
    try:
        html_path.write_text(await page.content(), encoding="utf-8")
        diagnostics["html_dump"] = str(html_path)
    except Exception as exc:
        diagnostics["html_dump_error"] = str(exc)
        logger.warning("Falha ao salvar HTML (%s): %s", label, exc)

    diagnostics["summary"] = _build_summary(diagnostics)
    return diagnostics


async def wait_for_visible_selector(
    page: Page,
    selector: str,
    *,
    timeout_seconds: int = 60,
    label: str = "dynamic_element",
    diagnostics_dir: Path | None = None,
    state: Literal["attached", "detached", "hidden", "visible"] = "visible",
) -> None:
    """Aguarda seletor visível; em timeout captura screenshot/DOM antes de falhar."""
    try:
        await page.wait_for_selector(
            selector,
            timeout=timeout_seconds * 1000,
            state=state,
        )
    except PlaywrightTimeoutError as exc:
        diagnostics = await capture_page_diagnostics(
            page,
            label=label,
            selector=selector,
            output_dir=diagnostics_dir,
        )
        logger.error(
            "%s: seletor '%s' não encontrado em %ss. %s",
            label,
            selector,
            timeout_seconds,
            diagnostics.get("summary"),
        )
        raise DynamicElementNotFoundError(
            selector,
            label,
            timeout_seconds=timeout_seconds,
            diagnostics=diagnostics,
        ) from exc
