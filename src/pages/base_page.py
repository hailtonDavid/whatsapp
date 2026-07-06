"""Base Page Object com seletores resilientes (RNF01)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from playwright.async_api import Locator, Page
from playwright.async_api import Error as PlaywrightError
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from wa_selectors import WA_URL


@dataclass(frozen=True)
class SelectorGroup:
    """Grupo de seletores CSS com fallbacks ordenados por prioridade."""

    name: str
    selectors: tuple[str, ...]

    def combined(self) -> str:
        return ", ".join(self.selectors)

    async def any_visible(self, page: Page, *, timeout_ms: int = 5_000) -> bool:
        return await self.first_visible(page, timeout_ms=timeout_ms) is not None

    async def first_visible(self, page: Page, *, timeout_ms: int = 5_000) -> Locator | None:
        if not self.selectors:
            return None

        per_attempt = max(800, timeout_ms // len(self.selectors))
        for selector in self.selectors:
            locator = page.locator(selector).first
            try:
                await locator.wait_for(state="visible", timeout=per_attempt)
                return locator
            except PlaywrightTimeoutError:
                continue
            except PlaywrightError:
                return None
        return None

    async def first_attached(self, page: Page, *, timeout_ms: int = 5_000) -> Locator | None:
        if not self.selectors:
            return None

        per_attempt = max(800, timeout_ms // len(self.selectors))
        for selector in self.selectors:
            locator = page.locator(selector).first
            try:
                await locator.wait_for(state="attached", timeout=per_attempt)
                return locator
            except PlaywrightTimeoutError:
                continue
            except PlaywrightError:
                return None
        return None


WaitUntil = Literal["commit", "domcontentloaded", "load", "networkidle"]


class BasePage:
    """Page Object base para WhatsApp Web."""

    path: str = WA_URL

    def __init__(self, page: Page) -> None:
        self.page = page

    async def goto(
        self,
        *,
        wait_until: WaitUntil = "domcontentloaded",
        timeout_ms: int = 60_000,
    ) -> None:
        await self.page.goto(self.path, wait_until=wait_until, timeout=timeout_ms)
