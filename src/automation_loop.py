"""Event loop asyncio dedicado para manter o Playwright vivo entre requests Flask."""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Coroutine
from typing import Any, TypeVar

T = TypeVar("T")

_lock = threading.Lock()
_loop: asyncio.AbstractEventLoop | None = None
_thread: threading.Thread | None = None


def get_automation_loop() -> asyncio.AbstractEventLoop:
    """Retorna (e cria, se necessário) um loop asyncio em thread de fundo."""
    global _loop, _thread
    with _lock:
        if _loop is not None and _loop.is_running():
            return _loop

        loop = asyncio.new_event_loop()

        def _runner() -> None:
            asyncio.set_event_loop(loop)
            loop.run_forever()

        thread = threading.Thread(target=_runner, name="whatsapp-automation", daemon=True)
        thread.start()
        _loop = loop
        _thread = thread
        return _loop


def run_automation_coroutine(coro: Coroutine[Any, Any, T], *, timeout: float | None = 300) -> T:
    """Executa coroutine Playwright no loop persistente (não fecha o browser ao retornar)."""
    loop = get_automation_loop()
    future = asyncio.run_coroutine_threadsafe(coro, loop)
    return future.result(timeout=timeout)
