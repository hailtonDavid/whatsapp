"""
WhatsApp Web Automation v8 — leitura, envio robusto e inventário de grupos.

Objetivo:
- Abrir automaticamente conversas configuradas em config/targets.json.
- Capturar mensagens visíveis e histórico via rolagem.
- Salvar apenas mensagens novas por alvo.
- Enviar mensagens para alvos configurados, com confirmação explícita.
- Listar grupos disponíveis no WhatsApp Web e gerar JSON de inventário/targets.
- Repetir continuamente em ciclos.

Uso:
    python src/whatsapp_auto_downloader.py doctor
    python src/whatsapp_auto_downloader.py unlock-profile --kill --kill-playwright --remove-locks
    python src/whatsapp_auto_downloader.py run-once --targets config/targets.json
    python src/whatsapp_auto_downloader.py scan --targets config/targets.json
    python src/whatsapp_auto_downloader.py send-once --targets config/targets.json --confirm
    python src/whatsapp_auto_downloader.py list-groups --output exports/groups/groups.json
    python src/whatsapp_auto_downloader.py list-contacts --output exports/contacts/contacts.json --targets config/targets.json

Regras de segurança:
- Use somente conta própria ou ambiente autorizado.
- Não burla QR Code, autenticação ou criptografia.
- Não tenta acessar mensagens fora da sessão autorizada do WhatsApp Web.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import hashlib
import json
import os
import platform
import re
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote

from dotenv import load_dotenv
from playwright.async_api import async_playwright, Page, BrowserContext, Playwright
from playwright.async_api import TimeoutError as PlaywrightTimeoutError


WA_URL = "https://web.whatsapp.com/"
PROJECT_ROOT = Path(__file__).resolve().parents[1]


def resolve_project_path(path: Path) -> Path:
    if path.is_absolute():
        return path
    return PROJECT_ROOT / path


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def safe_id(value: str) -> str:
    value = value.strip().lower()
    value = re.sub(r"[^a-z0-9_\-]+", "_", value, flags=re.I)
    value = re.sub(r"_+", "_", value).strip("_")
    return value or "alvo"


def str_to_bool(value: Optional[str], default: bool = False) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "sim", "s"}


@dataclass
class AppConfig:
    profile_dir: Path
    headless: bool
    ready_timeout: int
    export_dir: Path
    state_dir: Path
    browser_channel: str | None = None


def default_browser_channel() -> str | None:
    """Canal Playwright (Edge/Chrome instalado). Evita Chromium embutido bloqueado pelo WhatsApp."""
    raw = os.getenv("WA_BROWSER_CHANNEL", "").strip()
    if raw.lower() in {"none", "bundled", "chromium"}:
        return None
    if raw:
        return raw
    if platform.system().lower().startswith("win"):
        return "msedge"
    return "chrome"


def playwright_launch_kwargs(config: AppConfig) -> dict[str, Any]:
    args = [
        "--no-default-browser-check",
        "--disable-infobars",
        "--disable-blink-features=AutomationControlled",
    ]
    if not config.headless:
        args.extend(["--start-maximized", "--window-size=1440,900"])

    kwargs: dict[str, Any] = {
        "user_data_dir": str(config.profile_dir),
        "headless": config.headless,
        "viewport": {"width": 1440, "height": 900},
        "locale": "pt-BR",
        "args": args,
    }
    if config.browser_channel:
        kwargs["channel"] = config.browser_channel
    return kwargs


@dataclass
class Target:
    id: str
    type: str
    name: Optional[str] = None
    phone: Optional[str] = None
    enabled: bool = True
    send_enabled: bool = False
    message: Optional[str] = None


@dataclass
class TargetsConfig:
    interval_seconds: int = 60
    scrolls_per_target: int = 8
    delay_between_scrolls: float = 1.0
    delay_between_targets: float = 2.0
    append_only_new_messages: bool = True
    targets: List[Target] = field(default_factory=list)


def load_app_config(env_file: Path | None = None) -> AppConfig:
    load_dotenv(env_file or PROJECT_ROOT / ".env", encoding="utf-8-sig")

    profile_dir = Path(os.getenv("WA_PROFILE_DIR", "profile_whatsapp_v4"))
    if not profile_dir.is_absolute():
        profile_dir = PROJECT_ROOT / profile_dir

    export_dir = Path(os.getenv("WA_EXPORT_DIR", "exports"))
    if not export_dir.is_absolute():
        export_dir = PROJECT_ROOT / export_dir

    state_dir = Path(os.getenv("WA_STATE_DIR", "state"))
    if not state_dir.is_absolute():
        state_dir = PROJECT_ROOT / state_dir

    return AppConfig(
        profile_dir=profile_dir,
        headless=str_to_bool(os.getenv("WA_HEADLESS"), default=True),
        ready_timeout=int(os.getenv("WA_READY_TIMEOUT", "180")),
        export_dir=export_dir,
        state_dir=state_dir,
        browser_channel=default_browser_channel(),
    )


def load_targets_config(path: Path) -> TargetsConfig:
    if not path.exists():
        raise FileNotFoundError(f"Arquivo de alvos não encontrado: {path}")

    data = json.loads(path.read_text(encoding="utf-8"))

    targets: List[Target] = []
    for raw in data.get("targets", []):
        target_type = str(raw.get("type", "")).strip().lower()
        target_id = safe_id(str(raw.get("id") or raw.get("name") or raw.get("phone") or "alvo"))

        send_block = raw.get("send") if isinstance(raw.get("send"), dict) else {}
        message = raw.get("message") or send_block.get("message")
        send_enabled = bool(raw.get("send_enabled", send_block.get("enabled", False)))

        targets.append(Target(
            id=target_id,
            type=target_type,
            name=raw.get("name"),
            phone=str(raw.get("phone")).strip() if raw.get("phone") else None,
            enabled=bool(raw.get("enabled", True)),
            send_enabled=send_enabled,
            message=str(message) if message is not None else None,
        ))

    return TargetsConfig(
        interval_seconds=int(data.get("interval_seconds", 60)),
        scrolls_per_target=int(data.get("scrolls_per_target", 8)),
        delay_between_scrolls=float(data.get("delay_between_scrolls", 1.0)),
        delay_between_targets=float(data.get("delay_between_targets", 2.0)),
        append_only_new_messages=bool(data.get("append_only_new_messages", True)),
        targets=targets,
    )


class MessageState:
    def __init__(self, state_path: Path) -> None:
        self.state_path = state_path
        self.data: Dict[str, Any] = {"targets": {}}
        self.load()

    def load(self) -> None:
        if self.state_path.exists():
            try:
                self.data = json.loads(self.state_path.read_text(encoding="utf-8"))
                if "targets" not in self.data:
                    self.data["targets"] = {}
            except Exception:
                backup = self.state_path.with_suffix(".corrompido.json")
                self.state_path.replace(backup)
                self.data = {"targets": {}}

    def save(self) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.state_path.write_text(json.dumps(self.data, ensure_ascii=False, indent=2), encoding="utf-8")

    def seen_hashes(self, target_id: str) -> set[str]:
        raw = self.data.setdefault("targets", {}).setdefault(target_id, {})
        return set(raw.get("hashes", []))

    def add_hashes(self, target_id: str, hashes: List[str]) -> None:
        target_state = self.data.setdefault("targets", {}).setdefault(target_id, {})
        current = set(target_state.get("hashes", []))
        current.update(hashes)

        # Limita o estado para evitar arquivo gigante.
        ordered = list(current)
        if len(ordered) > 50000:
            ordered = ordered[-50000:]

        target_state["hashes"] = ordered
        target_state["updated_at"] = now_iso()


def lock_file_candidates(profile_dir: Path) -> List[Path]:
    names = ["SingletonLock", "SingletonCookie", "SingletonSocket", "LOCK", "lockfile"]
    return [profile_dir / name for name in names]


def existing_lock_files(profile_dir: Path) -> List[Path]:
    return [p for p in lock_file_candidates(profile_dir) if p.exists() or p.is_symlink()]


def remove_lock_files(profile_dir: Path) -> List[str]:
    removed: List[str] = []
    for item in lock_file_candidates(profile_dir):
        try:
            if item.exists() or item.is_symlink():
                item.unlink()
                removed.append(str(item))
        except Exception as exc:
            removed.append(f"FALHA: {item} -> {exc}")
    return removed


def normalize_path_variants(path: Path) -> List[str]:
    try:
        full = str(path.resolve())
    except Exception:
        full = str(path)
    return list({
        full,
        full.replace("\\", "/"),
        str(path),
        str(path).replace("\\", "/"),
    })


def find_profile_processes_windows(profile_dir: Path) -> List[Dict[str, Any]]:
    variants_json = json.dumps(normalize_path_variants(profile_dir), ensure_ascii=False)

    ps = f"""
$variants = ConvertFrom-Json @'
{variants_json}
'@
$items = Get-CimInstance Win32_Process | Where-Object {{
    $cmd = $_.CommandLine
    if (-not $cmd) {{ return $false }}
    foreach ($v in $variants) {{
        if ($cmd.Contains($v)) {{ return $true }}
    }}
    return $false
}} | Select-Object ProcessId, Name, CommandLine

$items | ConvertTo-Json -Depth 4
"""
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", ps],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=25,
        )
        out = result.stdout.strip()
        if not out:
            return []
        parsed = json.loads(out)
        if isinstance(parsed, dict):
            return [parsed]
        if isinstance(parsed, list):
            return parsed
        return []
    except Exception:
        return []


def find_playwright_chromium_processes_windows() -> List[Dict[str, Any]]:
    ps = r"""
$items = Get-CimInstance Win32_Process | Where-Object {
    $cmd = $_.CommandLine
    if (-not $cmd) { return $false }
    ($cmd -like "*ms-playwright*") -and
    (($cmd -like "*chrome.exe*") -or ($cmd -like "*chromium*")) -and
    (($cmd -like "*--remote-debugging-pipe*") -or ($cmd -like "*--user-data-dir*"))
} | Select-Object ProcessId, Name, CommandLine

$items | ConvertTo-Json -Depth 4
"""
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", ps],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=25,
        )
        out = result.stdout.strip()
        if not out:
            return []
        parsed = json.loads(out)
        if isinstance(parsed, dict):
            return [parsed]
        if isinstance(parsed, list):
            return parsed
        return []
    except Exception:
        return []


def kill_processes_windows(processes: List[Dict[str, Any]]) -> int:
    killed = 0
    for proc in processes:
        pid = proc.get("ProcessId")
        if not pid:
            continue
        try:
            subprocess.run(
                ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", f"Stop-Process -Id {int(pid)} -Force"],
                capture_output=True,
                text=True,
                timeout=10,
            )
            killed += 1
        except Exception:
            pass
    return killed


def find_profile_processes_posix(profile_dir: Path) -> List[Dict[str, Any]]:
    variants = normalize_path_variants(profile_dir)
    items: List[Dict[str, Any]] = []

    try:
        result = subprocess.run(["ps", "axo", "pid=,command="], capture_output=True, text=True, timeout=10)
        for line in result.stdout.splitlines():
            if any(v in line for v in variants):
                pid, _, cmd = line.strip().partition(" ")
                items.append({"ProcessId": pid, "Name": "process", "CommandLine": cmd})
    except Exception:
        pass

    return items


def kill_processes_posix(processes: List[Dict[str, Any]]) -> int:
    killed = 0
    for proc in processes:
        pid = str(proc.get("ProcessId", "")).strip()
        if not pid:
            continue
        try:
            subprocess.run(["kill", "-9", pid], timeout=5)
            killed += 1
        except Exception:
            pass
    return killed


def find_profile_processes(profile_dir: Path) -> List[Dict[str, Any]]:
    if platform.system().lower().startswith("win"):
        return find_profile_processes_windows(profile_dir)
    return find_profile_processes_posix(profile_dir)


def kill_processes(processes: List[Dict[str, Any]]) -> int:
    if platform.system().lower().startswith("win"):
        return kill_processes_windows(processes)
    return kill_processes_posix(processes)


def profile_error_message(profile_dir: Path, original_error: BaseException) -> str:
    return f"""
Não consegui abrir o perfil persistente do Chromium.

Perfil usado:
  {profile_dir}

Correção recomendada:
  python src\\whatsapp_auto_downloader.py unlock-profile --kill --kill-playwright --remove-locks

Depois execute novamente:
  python src\\whatsapp_auto_downloader.py scan --targets config\\targets.json

Erro original:
  {type(original_error).__name__}: {original_error}
""".strip()


async def open_whatsapp(config: AppConfig) -> Tuple[Playwright, BrowserContext, Page]:
    config.profile_dir.mkdir(parents=True, exist_ok=True)

    playwright = await async_playwright().start()
    try:
        context = await playwright.chromium.launch_persistent_context(
            **playwright_launch_kwargs(config),
        )
    except Exception as exc:
        await playwright.stop()
        msg = str(exc)
        if (
            "Target page, context or browser has been closed" in msg
            or "Abrindo em uma sessão de navegador existente" in msg
            or "Opening in existing browser session" in msg
            or "ProcessSingleton" in msg
            or "session" in msg.lower()
        ):
            raise RuntimeError(profile_error_message(config.profile_dir, exc)) from exc
        raise

    page = context.pages[0] if context.pages else await context.new_page()
    await page.goto(WA_URL, wait_until="domcontentloaded")
    if not config.headless:
        try:
            await page.bring_to_front()
        except Exception:
            pass
    return playwright, context, page


async def wait_for_whatsapp_ready(page: Page, timeout_seconds: int) -> None:
    print("Abrindo WhatsApp Web.")
    print("Se aparecer QR Code, escaneie normalmente com seu celular autorizado.")

    try:
        await page.wait_for_function(
            """
            () => {
                const text = document.body?.innerText || "";
                const hasWhatsapp = text.toLowerCase().includes("whatsapp");
                const hasEditable = document.querySelectorAll(
                    "div[contenteditable='true'], [role='textbox']"
                ).length > 0;
                const hasMessages = document.querySelectorAll(
                    "div.copyable-text[data-pre-plain-text], div[data-pre-plain-text], div.message-in, div.message-out"
                ).length > 0;
                const hasSidePane = document.querySelectorAll("[aria-label], [role='grid'], [role='listbox']").length > 0;
                return hasWhatsapp && (hasEditable || hasMessages || hasSidePane);
            }
            """,
            timeout=timeout_seconds * 1000,
        )
        print("Interface carregada.")
    except PlaywrightTimeoutError:
        print("Não foi possível confirmar o carregamento dentro do tempo limite.")
        print("Verifique QR Code, conexão e se o WhatsApp Web abriu corretamente.")


async def wait_for_whatsapp_sync_idle(page: Page, *, timeout_ms: int = 120_000) -> None:
    """Aguarda o fim da sincronização inicial ('Sincronizando conversas...')."""
    markers = (
        "sincronizando conversas",
        "synchronizing chats",
        "syncing chats",
        "carregando conversas",
    )
    deadline = time.monotonic() + (timeout_ms / 1000)
    while time.monotonic() < deadline:
        try:
            body_text = await page.evaluate("() => (document.body?.innerText || '').toLowerCase()")
        except Exception:
            body_text = ""
        if not any(marker in body_text for marker in markers):
            return
        print("WhatsApp ainda sincronizando conversas — aguardando...")
        await page.wait_for_timeout(1500)


def _name_search_queries(name: str) -> List[str]:
    """Gera variantes de busca (nome completo, trecho antes da vírgula, primeiras palavras)."""
    cleaned = (name or "").strip()
    if not cleaned:
        return []

    queries: List[str] = [cleaned]
    if "," in cleaned:
        head = cleaned.split(",", 1)[0].strip()
        if head:
            queries.append(head)
    words = cleaned.split()
    if len(words) >= 2:
        queries.append(" ".join(words[:2]))

    seen: set[str] = set()
    unique: List[str] = []
    for item in queries:
        key = item.casefold()
        if key and key not in seen:
            seen.add(key)
            unique.append(item)
    return unique


async def clear_search(page: Page) -> None:
    await page.keyboard.press("Escape")
    await page.wait_for_timeout(300)


FIND_SEARCH_BOX_JS = """
() => {
    const visible = (el) => {
        const r = el.getBoundingClientRect();
        const s = window.getComputedStyle(el);
        return r.width > 0 && r.height > 0 && s.display !== "none" && s.visibility !== "hidden";
    };

    const boxes = Array.from(document.querySelectorAll(
        "div[contenteditable='true'][role='textbox'], div[contenteditable='true'], [role='textbox']"
    ))
    .filter(visible)
    .map((el, index) => {
        const r = el.getBoundingClientRect();
        const label = [
            el.getAttribute("aria-label") || "",
            el.getAttribute("title") || "",
            el.getAttribute("data-tab") || "",
            el.innerText || ""
        ].join(" ").toLowerCase();

        let score = 0;
        if (r.x < window.innerWidth * 0.45) score += 4;
        if (label.includes("pesquisar") || label.includes("search") || label.includes("busca")) score += 6;
        if (r.y < window.innerHeight * 0.35) score += 2;
        if (r.width > 100) score += 1;

        return {
            index,
            score,
            x: r.x,
            y: r.y,
            width: r.width,
            height: r.height,
            label
        };
    })
    .sort((a, b) => b.score - a.score);

    return boxes[0] || null;
}
"""


async def click_search_box(page: Page) -> bool:
    box = await page.evaluate(FIND_SEARCH_BOX_JS)
    if not box:
        return False

    await page.mouse.click(
        box["x"] + min(40, box["width"] / 2),
        box["y"] + box["height"] / 2,
    )
    await page.wait_for_timeout(300)
    return True


FIND_CHAT_RESULT_JS = """
(params) => {
    const queryRaw = typeof params === "string" ? params : (params?.query || params?.fullName || "");
    const fullNameRaw = typeof params === "string" ? params : (params?.fullName || params?.query || "");
    const query = String(queryRaw || "").trim().toLowerCase();
    const fullName = String(fullNameRaw || "").trim().toLowerCase();

    const normalize = (txt) => String(txt || "")
        .replace(/\\s+/g, " ")
        .trim()
        .toLowerCase();

    const visible = (el) => {
        const r = el.getBoundingClientRect();
        const s = window.getComputedStyle(el);
        return r.width > 0 && r.height > 0 &&
               s.display !== "none" &&
               s.visibility !== "hidden" &&
               r.x < window.innerWidth * 0.62 &&
               r.y > 40;
    };

    const inMensagensSection = (el) => {
        let node = el;
        for (let depth = 0; depth < 10 && node; depth++) {
            const text = normalize(node.textContent || "");
            if (text === "mensagens" || text.startsWith("mensagens\\n")) return true;
            node = node.parentElement;
        }
        return false;
    };

    const scoreCandidate = (el, labelText) => {
        const r = el.getBoundingClientRect();
        const text = normalize(labelText || el.innerText || el.textContent || "");
        const title = normalize(el.getAttribute?.("title") || "");
        const aria = normalize(el.getAttribute?.("aria-label") || "");
        const joined = [text, title, aria].filter(Boolean).join(" | ");

        let score = 0;
        const target = fullName || query;
        if (title && title === target) score += 220;
        if (title && title === query) score += 200;
        if (text && text === target) score += 180;
        if (title && title.includes(query)) score += 120;
        if (text && text.includes(query)) score += 90;
        if (joined.includes(query)) score += 60;
        if (target && title.includes(target)) score += 80;
        if (el.matches?.("span[title], div[title]")) score += 40;
        if (el.closest?.("#pane-side")) score += 25;
        if (el.closest?.("[data-testid='cell-frame-container'], [role='listitem'], [role='row']")) score += 20;
        if (r.x < window.innerWidth * 0.45) score += 10;
        if (inMensagensSection(el)) score -= 120;
        if (text.includes("mensagens") && text.length < 24) score -= 80;

        return { score, text, title, aria, x: r.x, y: r.y, width: r.width, height: r.height };
    };

    const pane = document.querySelector("#pane-side") || document.body;
    const seen = new Set();
    const candidates = [];

    for (const el of pane.querySelectorAll("span[title], div[title], [role='listitem'], [role='row'], [data-testid='cell-frame-container']")) {
        if (!visible(el)) continue;
        const label = el.getAttribute("title") || el.getAttribute("aria-label") || el.innerText || "";
        const item = scoreCandidate(el, label);
        if (item.score < 60) continue;
        const key = `${item.title}|${item.text}|${Math.round(item.y)}`;
        if (seen.has(key)) continue;
        seen.add(key);
        candidates.push(item);
    }

    candidates.sort((a, b) => b.score - a.score || a.y - b.y);
    return candidates[0] || null;
}
"""


async def open_chat_by_name(page: Page, name: str) -> tuple[bool, Optional[str]]:
    print(f"Buscando conversa: {name}")

    await wait_for_whatsapp_sync_idle(page, timeout_ms=60_000)

    last_error = "Nenhum resultado encontrado na pesquisa."
    for query in _name_search_queries(name):
        await clear_search(page)

        ok = await click_search_box(page)
        if not ok:
            last_error = "Não encontrei a caixa de pesquisa."
            print(last_error)
            continue

        await page.keyboard.press("Control+A")
        await page.keyboard.type(query, delay=20)
        await page.wait_for_timeout(2500)

        result = await page.evaluate(
            FIND_CHAT_RESULT_JS,
            {"query": query, "fullName": name},
        )
        if result:
            await page.mouse.click(
                result["x"] + min(50, result["width"] / 2),
                result["y"] + result["height"] / 2,
            )
            await page.wait_for_timeout(2000)
        else:
            await page.keyboard.press("Enter")
            await page.wait_for_timeout(2000)

        ready = await wait_for_chat_send_ready(page, timeout_ms=12_000)
        if ready.get("ok"):
            return True, None

        last_error = str(ready.get("error") or f"Nenhum resultado encontrado para: {query}")
        print(f"Busca '{query}' não abriu conversa: {last_error}")

    return False, last_error


async def wait_for_chat_send_ready(page: Page, timeout_ms: int = 30000) -> Dict[str, Any]:
    """Aguarda a conversa ficar pronta para digitação e detecta erros de telefone.

    A versão anterior retornava sucesso logo após abrir a URL /send?phone=...
    mesmo quando o WhatsApp Web ainda estava em tela intermediária ou mostrava erro
    de número inválido. Isso fazia o log marcar como enviado sem envio real.
    """
    deadline = time.monotonic() + (timeout_ms / 1000)
    last_state: Dict[str, Any] = {}

    while time.monotonic() < deadline:
        try:
            state = await page.evaluate(CHAT_SEND_STATE_JS)
        except Exception as exc:
            state = {"ok": False, "error": f"Falha ao avaliar tela do WhatsApp: {exc}"}

        last_state = state or {}
        if last_state.get("unsupported_browser"):
            channel_hint = os.getenv("WA_BROWSER_CHANNEL") or "msedge (Windows) / chrome"
            return {
                "ok": False,
                "error": (
                    "WhatsApp Web rejeitou o navegador automatizado. "
                    f"Configure WA_BROWSER_CHANNEL={channel_hint} no .env e reinicie. "
                    "Se persistir, use WA_HEADLESS=false no painel (Abrir visível)."
                ),
                "state": last_state,
            }
        if last_state.get("invalid_reason"):
            return {
                "ok": False,
                "error": f"WhatsApp informou erro no destino: {last_state.get('invalid_reason')}",
                "state": last_state,
            }
        if last_state.get("ok"):
            return {"ok": True, "state": last_state}

        await page.wait_for_timeout(700)

    return {
        "ok": False,
        "error": "A conversa não ficou pronta para envio dentro do tempo limite.",
        "state": last_state,
    }


async def open_chat_by_phone(
    page: Page,
    phone: str,
    message: Optional[str] = None,
) -> tuple[bool, Optional[str]]:
    clean = re.sub(r"\D+", "", phone or "")
    if not clean:
        print("Telefone vazio/inválido.")
        return False, "Telefone vazio/inválido."

    print(f"Abrindo conversa por telefone: {clean}")
    url = f"{WA_URL}send?phone={clean}&app_absent=0"
    # Para telefone, o WhatsApp Web costuma ser mais confiável quando a mensagem
    # também é enviada pela URL oficial. Mesmo assim, o script valida/reforça o
    # preenchimento da caixa antes de clicar em Enviar.
    if message:
        url += "&text=" + quote(message)

    await page.goto(url, wait_until="domcontentloaded")
    await page.wait_for_timeout(3500)

    # Se aparecer botão de continuar/usar WhatsApp Web, tenta confirmar.
    try:
        for text in ["Continuar", "Continue", "Usar o WhatsApp Web", "use WhatsApp Web", "Iniciar conversa", "Start chat"]:
            locator = page.get_by_text(text, exact=False)
            if await locator.count() > 0:
                await locator.first.click(timeout=2500)
                await page.wait_for_timeout(3000)
                break
    except Exception:
        pass

    ready = await wait_for_chat_send_ready(page, timeout_ms=30000)
    if not ready.get("ok"):
        error = str(ready.get("error") or "A conversa não ficou pronta para envio.")
        print(f"Conversa por telefone não ficou pronta: {error}")
        return False, error

    return True, None


async def open_target(page: Page, target: Target) -> tuple[bool, Optional[str]]:
    if target.type in {"group", "contact", "name"}:
        if not target.name:
            print(f"Alvo {target.id} sem campo name.")
            return False, f"Alvo {target.id} sem campo name."
        opened, open_error = await open_chat_by_name(page, target.name)
        return opened, open_error if not opened else None

    if target.type == "phone":
        if not target.phone:
            print(f"Alvo {target.id} sem campo phone.")
            return False, f"Alvo {target.id} sem campo phone."
        return await open_chat_by_phone(page, target.phone)

    print(f"Tipo de alvo não suportado: {target.type}")
    return False, f"Tipo de alvo não suportado: {target.type}"


FIND_MESSAGE_BOX_JS = r"""
() => {
    const visible = (el) => {
        const r = el.getBoundingClientRect();
        const s = window.getComputedStyle(el);
        return r.width > 0 &&
               r.height > 0 &&
               s.display !== "none" &&
               s.visibility !== "hidden" &&
               r.bottom >= 0 &&
               r.right >= 0 &&
               r.top <= window.innerHeight &&
               r.left <= window.innerWidth;
    };

    const normalize = (txt) => String(txt || "").replace(/\s+/g, " ").trim().toLowerCase();

    // Prioriza a caixa real de composição dentro do rodapé do chat. A versão anterior
    // podia selecionar a pesquisa lateral em alguns layouts do WhatsApp Web.
    const selectors = [
        "footer div[contenteditable='true'][role='textbox']",
        "footer div[contenteditable='true']",
        "main footer div[contenteditable='true'][role='textbox']",
        "main footer div[contenteditable='true']",
        "div[contenteditable='true'][aria-label*='mensagem' i]",
        "div[contenteditable='true'][aria-label*='message' i]",
        "div[contenteditable='true'][title*='mensagem' i]",
        "div[contenteditable='true'][title*='message' i]",
        "div[contenteditable='true'][role='textbox']",
        "div[contenteditable='true']"
    ];

    const seen = new Set();
    const rawCandidates = [];
    for (const selector of selectors) {
        for (const el of Array.from(document.querySelectorAll(selector))) {
            if (seen.has(el)) continue;
            seen.add(el);
            rawCandidates.push(el);
        }
    }

    const candidates = rawCandidates
    .filter(visible)
    .map((el) => {
        const r = el.getBoundingClientRect();
        const label = normalize([
            el.getAttribute("aria-label") || "",
            el.getAttribute("title") || "",
            el.getAttribute("data-tab") || "",
            el.innerText || ""
        ].join(" "));

        let score = 0;

        if (el.closest("footer")) score += 40;
        if (el.closest("main")) score += 12;
        if (r.x > window.innerWidth * 0.25) score += 8;
        if (r.y > window.innerHeight * 0.55) score += 8;
        if (r.width > 200) score += 4;
        if (label.includes("mensagem") || label.includes("message")) score += 12;
        if (label.includes("digite") || label.includes("type")) score += 6;

        // Evita capturar pesquisa lateral, campo de busca de emojis ou filtros.
        if (label.includes("pesquisar") || label.includes("search") || label.includes("buscar")) score -= 35;
        if (r.x < window.innerWidth * 0.22) score -= 25;
        if (r.y < window.innerHeight * 0.35) score -= 15;

        return {
            score,
            x: r.x,
            y: r.y,
            width: r.width,
            height: r.height,
            label,
            text: el.innerText || el.textContent || ""
        };
    })
    .filter(x => x.score > 0)
    .sort((a, b) => b.score - a.score || b.y - a.y);

    return candidates[0] || null;
}
"""


FIND_SEND_BUTTON_JS = r"""
() => {
    const visible = (el) => {
        const r = el.getBoundingClientRect();
        const s = window.getComputedStyle(el);
        return r.width > 0 &&
               r.height > 0 &&
               s.display !== "none" &&
               s.visibility !== "hidden" &&
               r.bottom >= 0 &&
               r.right >= 0 &&
               r.top <= window.innerHeight &&
               r.left <= window.innerWidth;
    };

    const normalize = (txt) => String(txt || "").replace(/\s+/g, " ").trim().toLowerCase();

    const raw = Array.from(document.querySelectorAll(
        "span[data-icon='send'], button[aria-label*='Enviar' i], button[aria-label*='Send' i], " +
        "div[role='button'][aria-label*='Enviar' i], div[role='button'][aria-label*='Send' i], " +
        "button, div[role='button']"
    ));

    const candidates = raw
        .map((node) => {
            let el = node;
            if (node.matches && node.matches("span[data-icon='send']")) {
                el = node.closest("button, div[role='button']") || node.parentElement || node;
            }
            return { node, el };
        })
        .filter(({ el }) => el && visible(el))
        .map(({ node, el }) => {
            const r = el.getBoundingClientRect();
            const label = normalize([
                el.getAttribute("aria-label") || "",
                el.getAttribute("title") || "",
                el.getAttribute("data-testid") || "",
                el.getAttribute("data-icon") || "",
                node.getAttribute?.("data-icon") || "",
                el.innerText || ""
            ].join(" "));

            let score = 0;
            if (el.closest("footer")) score += 20;
            if (r.x > window.innerWidth * 0.50) score += 4;
            if (r.y > window.innerHeight * 0.55) score += 4;
            if (label.includes("enviar") || label.includes("send")) score += 20;
            if (label.includes("compose-btn-send")) score += 10;
            if (node.matches && node.matches("span[data-icon='send']")) score += 30;

            return {
                score,
                x: r.x,
                y: r.y,
                width: r.width,
                height: r.height,
                label
            };
        })
        .filter(x => x.score >= 18)
        .sort((a, b) => b.score - a.score || b.x - a.x);

    return candidates[0] || null;
}
"""

CHAT_SEND_STATE_JS = r"""
() => {
    const bodyText = String(document.body?.innerText || "").toLowerCase();
    const invalidMarkers = [
        "número de telefone compartilhado por url é inválido",
        "numero de telefone compartilhado por url é inválido",
        "phone number shared via url is invalid",
        "número de telefone inválido",
        "numero de telefone inválido",
        "invalid phone number",
        "não está no whatsapp",
        "not on whatsapp"
    ];

    const invalidReason = invalidMarkers.find(marker => bodyText.includes(marker)) || null;

    const unsupportedMarkers = [
        "funciona no google chrome",
        "works on google chrome",
        "atualize o chrome",
        "update chrome",
        "atualize o google chrome",
        "update google chrome",
        "browser is not supported",
        "navegador não é suportado",
        "navegador nao e suportado"
    ];
    const unsupported_browser = unsupportedMarkers.some(marker => bodyText.includes(marker));

    const visible = (el) => {
        const r = el.getBoundingClientRect();
        const s = window.getComputedStyle(el);
        return r.width > 0 && r.height > 0 && s.display !== "none" && s.visibility !== "hidden";
    };

    const hasMessageBox = Array.from(document.querySelectorAll(
        "footer div[contenteditable='true'], main footer div[contenteditable='true'], " +
        "div[contenteditable='true'][aria-label*='mensagem' i], div[contenteditable='true'][aria-label*='message' i]"
    )).some(visible);

    const hasQr = bodyText.includes("use o whatsapp no seu computador") ||
                  bodyText.includes("use whatsapp on your computer") ||
                  bodyText.includes("qr code");

    return {
        ok: !invalidReason && !unsupported_browser && hasMessageBox,
        has_message_box: hasMessageBox,
        invalid_reason: invalidReason,
        unsupported_browser,
        has_qr: hasQr,
        body_preview: bodyText.slice(0, 500)
    };
}
"""

GET_DRAFT_TEXT_JS = r"""
() => {
    const visible = (el) => {
        const r = el.getBoundingClientRect();
        const s = window.getComputedStyle(el);
        return r.width > 0 && r.height > 0 && s.display !== "none" && s.visibility !== "hidden";
    };

    const candidates = Array.from(document.querySelectorAll(
        "footer div[contenteditable='true'][role='textbox'], footer div[contenteditable='true'], " +
        "main footer div[contenteditable='true'][role='textbox'], main footer div[contenteditable='true'], " +
        "div[contenteditable='true'][aria-label*='mensagem' i], div[contenteditable='true'][aria-label*='message' i]"
    )).filter(visible);

    const el = candidates[candidates.length - 1] || null;
    if (!el) return "";
    return el.innerText || el.textContent || "";
}
"""

COUNT_OUTGOING_MESSAGE_MATCHES_JS = r"""
(expected) => {
    const normalize = (txt) => String(txt || "")
        .replace(/\u200e/g, "")
        .replace(/\u200f/g, "")
        .replace(/\s+/g, " ")
        .trim();

    const expectedText = normalize(expected);
    const visible = (el) => {
        const r = el.getBoundingClientRect();
        const s = window.getComputedStyle(el);
        return r.width > 0 && r.height > 0 &&
               s.display !== "none" && s.visibility !== "hidden" &&
               r.bottom >= 0 && r.right >= 0 && r.top <= window.innerHeight && r.left <= window.innerWidth;
    };

    const getText = (el) => {
        const nodes = Array.from(el.querySelectorAll(
            "span.selectable-text.copyable-text, span.selectable-text, span[dir='ltr'], span[dir='auto']"
        ));
        const texts = nodes.map(n => normalize(n.innerText || n.textContent || "")).filter(Boolean);
        if (texts.length) return normalize(texts.join("\n"));
        return normalize(el.innerText || el.textContent || "");
    };

    const getStatus = (el) => {
        const statusEl = el.querySelector(
            "span[data-icon^='msg-'], span[aria-label*='Enviada' i], span[aria-label*='Entregue' i], " +
            "span[aria-label*='Lida' i], span[aria-label*='Sent' i], span[aria-label*='Delivered' i], span[aria-label*='Read' i]"
        );
        if (!statusEl) return { icon: "", label: "" };
        return {
            icon: statusEl.getAttribute("data-icon") || "",
            label: statusEl.getAttribute("aria-label") || statusEl.getAttribute("title") || ""
        };
    };

    const messages = Array.from(document.querySelectorAll("div.message-out"))
        .filter(visible);

    let count = 0;
    let last_text = "";
    let last_status = { icon: "", label: "" };
    let last_match_text = "";
    let last_match_status = { icon: "", label: "" };

    for (const el of messages) {
        const text = getText(el);
        if (!text) continue;
        last_text = text;
        last_status = getStatus(el);
        if (normalize(text) === expectedText || normalize(text).includes(expectedText)) {
            count += 1;
            last_match_text = text;
            last_match_status = getStatus(el);
        }
    }
    return { count, last_text, last_status, last_match_text, last_match_status };
}
"""

def normalize_for_compare(value: str) -> str:
    return re.sub(r"\s+", " ", (value or "").replace("\u200e", "").replace("\u200f", "")).strip()


async def click_message_box(page: Page) -> bool:
    box = await page.evaluate(FIND_MESSAGE_BOX_JS)
    if not box:
        return False

    await page.mouse.click(
        box["x"] + min(80, box["width"] / 2),
        box["y"] + box["height"] / 2,
    )
    await page.wait_for_timeout(300)
    return True


async def get_draft_text(page: Page) -> str:
    try:
        return str(await page.evaluate(GET_DRAFT_TEXT_JS) or "")
    except Exception:
        return ""


async def count_outgoing_message_matches(page: Page, message: str) -> Dict[str, Any]:
    try:
        result = await page.evaluate(COUNT_OUTGOING_MESSAGE_MATCHES_JS, message)
        if isinstance(result, dict):
            return result
    except Exception:
        pass
    return {"count": 0, "last_text": ""}


async def clear_current_draft(page: Page) -> None:
    await page.keyboard.press("Control+A")
    await page.keyboard.press("Backspace")
    await page.wait_for_timeout(180)


async def type_multiline_message(page: Page, message: str) -> None:
    lines = message.splitlines() or [message]
    for index, line in enumerate(lines):
        if line:
            await page.keyboard.type(line, delay=15)
        if index < len(lines) - 1:
            await page.keyboard.press("Shift+Enter")
            await page.wait_for_timeout(80)


async def fill_message_box_reliably(page: Page, text: str, *, clear_draft: bool = True) -> Dict[str, Any]:
    expected = normalize_for_compare(text)

    focused = await click_message_box(page)
    if not focused:
        return {"ok": False, "error": "Não encontrei a caixa de mensagem do chat."}

    if clear_draft:
        await clear_current_draft(page)

    # 1) Primeiro tenta insert_text, que costuma ser mais confiável para contenteditable.
    try:
        await page.keyboard.insert_text(text)
        await page.wait_for_timeout(450)
        draft = normalize_for_compare(await get_draft_text(page))
        if draft == expected or expected in draft:
            return {"ok": True, "method": "keyboard.insert_text", "draft_text": draft}
    except Exception:
        pass

    # 2) Fallback com digitação linha a linha, preservando múltiplas linhas com Shift+Enter.
    try:
        await clear_current_draft(page)
        await type_multiline_message(page, text)
        await page.wait_for_timeout(450)
        draft = normalize_for_compare(await get_draft_text(page))
        if draft == expected or expected in draft:
            return {"ok": True, "method": "keyboard.type", "draft_text": draft}
    except Exception:
        pass

    # 3) Fallback usando fill no contenteditable do footer.
    try:
        await clear_current_draft(page)
        locator = page.locator("footer div[contenteditable='true']").last
        # Compatibilidade com versões antigas/novas do Playwright: em algumas, .last é método.
        if callable(locator):
            locator = locator()
        await locator.fill(text, timeout=5000)
        await page.wait_for_timeout(450)
        draft = normalize_for_compare(await get_draft_text(page))
        if draft == expected or expected in draft:
            return {"ok": True, "method": "locator.fill", "draft_text": draft}
    except Exception:
        pass

    draft_after = normalize_for_compare(await get_draft_text(page))
    return {
        "ok": False,
        "error": "Não consegui confirmar que o texto entrou na caixa de mensagem correta.",
        "draft_text": draft_after,
    }


async def click_send_button_or_press_enter(page: Page) -> str:
    """Clica no botão real de envio antes de usar Enter como fallback.

    O WhatsApp Web muda seletores com frequência. Por isso usamos camadas:
    1. locators diretos no footer;
    2. coordenada calculada por JS;
    3. Enter apenas como último recurso.
    """
    await page.wait_for_timeout(500)

    selectors = [
        "footer button[aria-label*='Enviar' i]",
        "footer button[aria-label*='Send' i]",
        "footer div[role='button'][aria-label*='Enviar' i]",
        "footer div[role='button'][aria-label*='Send' i]",
        "footer button:has(span[data-icon='send'])",
        "footer div[role='button']:has(span[data-icon='send'])",
        "button:has(span[data-icon='send'])",
        "div[role='button']:has(span[data-icon='send'])",
        "span[data-icon='send']",
    ]

    for selector in selectors:
        try:
            locator = page.locator(selector)
            count = await locator.count()
            if count <= 0:
                continue
            await locator.nth(count - 1).click(timeout=3500, force=False)
            await page.wait_for_timeout(700)
            return f"locator:{selector}"
        except Exception:
            continue

    button = await page.evaluate(FIND_SEND_BUTTON_JS)
    if button:
        await page.mouse.click(
            button["x"] + button["width"] / 2,
            button["y"] + button["height"] / 2,
        )
        await page.wait_for_timeout(700)
        return f"coordinate:{button.get('label') or 'send'}"

    await page.keyboard.press("Enter")
    await page.wait_for_timeout(700)
    return "enter"

async def wait_until_message_is_verified_sent(
    page: Page,
    text: str,
    *,
    before_count: int,
    timeout_ms: int = 25000,
) -> Dict[str, Any]:
    deadline = time.monotonic() + (timeout_ms / 1000)
    last_check: Dict[str, Any] = {}
    saw_new_message = False

    pending_icons = {"msg-time", "msg-dblcheck-ack-pending"}

    while time.monotonic() < deadline:
        matches = await count_outgoing_message_matches(page, text)
        last_check = matches
        after_count = int(matches.get("count") or 0)

        if after_count > before_count:
            saw_new_message = True
            status = matches.get("last_match_status") or {}
            icon = str(status.get("icon") or "")
            label = str(status.get("label") or "")

            # Se a mensagem ainda está com relógio, ela apareceu no chat, mas o WhatsApp
            # ainda não confirmou envio ao servidor. Continua aguardando.
            if icon not in pending_icons:
                return {
                    "ok": True,
                    "verification": "outgoing_message_visible_and_not_pending",
                    "matches_before": before_count,
                    "matches_after": after_count,
                    "last_text": matches.get("last_match_text") or matches.get("last_text"),
                    "delivery_status_icon": icon,
                    "delivery_status_label": label,
                }

        try:
            state = await page.evaluate(CHAT_SEND_STATE_JS)
            if state.get("invalid_reason"):
                return {
                    "ok": False,
                    "error": f"WhatsApp informou erro no destino: {state.get('invalid_reason')}",
                    "state": state,
                    "last_check": last_check,
                }
        except Exception:
            pass

        await page.wait_for_timeout(800)

    draft_after = normalize_for_compare(await get_draft_text(page))
    if saw_new_message:
        return {
            "ok": False,
            "error": "A mensagem apareceu no chat, mas ficou pendente com status de envio. Verifique conexão do WhatsApp Web/celular.",
            "verification": "visible_but_pending",
            "matches_before": before_count,
            "matches_after": int(last_check.get("count") or 0),
            "last_text": last_check.get("last_match_text") or last_check.get("last_text"),
            "last_status": last_check.get("last_match_status"),
            "draft_text_after": draft_after,
        }

    return {
        "ok": False,
        "error": "O clique/Enter foi executado, mas a mensagem não apareceu como enviada no chat. Não considerei como enviada.",
        "verification": "not_visible_after_send",
        "matches_before": before_count,
        "matches_after": int(last_check.get("count") or 0),
        "last_text": last_check.get("last_match_text") or last_check.get("last_text"),
        "last_status": last_check.get("last_match_status"),
        "draft_text_after": draft_after,
    }

async def send_text_to_current_chat(page: Page, message: str, *, clear_draft: bool = True) -> Dict[str, Any]:
    text = (message or "").strip()
    if not text:
        return {"ok": False, "error": "Mensagem vazia."}

    ready = await wait_for_chat_send_ready(page, timeout_ms=30000)
    if not ready.get("ok"):
        return {
            "ok": False,
            "error": ready.get("error") or "A conversa não está pronta para envio.",
            "chat_state": ready.get("state"),
        }

    before = await count_outgoing_message_matches(page, text)
    before_count = int(before.get("count") or 0)

    expected = normalize_for_compare(text)
    existing_draft = normalize_for_compare(await get_draft_text(page))
    if existing_draft == expected or (expected and expected in existing_draft):
        filled = {"ok": True, "method": "existing_prefilled_draft", "draft_text": existing_draft}
    else:
        filled = await fill_message_box_reliably(page, text, clear_draft=clear_draft)
        if not filled.get("ok"):
            return {
                "ok": False,
                "error": filled.get("error"),
                "draft_text_after": filled.get("draft_text"),
            }

    send_method = await click_send_button_or_press_enter(page)
    verified = await wait_until_message_is_verified_sent(page, text, before_count=before_count)

    if not verified.get("ok"):
        verified.update({
            "send_method": send_method,
            "message_chars": len(text),
            "filled_method": filled.get("method"),
        })
        return verified

    return {
        "ok": True,
        "verified": True,
        "verification": verified.get("verification"),
        "send_method": send_method,
        "message_chars": len(text),
        "filled_method": filled.get("method"),
        "sent_at": now_iso(),
        "matches_before": verified.get("matches_before"),
        "matches_after": verified.get("matches_after"),
    }


MEDIA_FILE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".mp4", ".mov", ".avi", ".mkv", ".webm"}


def attachment_prefers_media_menu(path: Path) -> bool:
    return path.suffix.lower() in MEDIA_FILE_EXTENSIONS


async def _set_files_on_input(page: Page, file_path: Path) -> bool:
    selectors = [
        'footer input[type="file"]',
        'input[type="file"][accept*="image"]',
        'input[type="file"][accept*="*"]',
        'input[type="file"]',
    ]
    for selector in selectors:
        try:
            locator = page.locator(selector)
            count = await locator.count()
            for index in range(count):
                try:
                    await locator.nth(index).set_input_files(str(file_path), timeout=4000)
                    await page.wait_for_timeout(900)
                    return True
                except Exception:
                    continue
        except Exception:
            continue
    return False


async def _click_first_visible(page: Page, selectors: tuple[str, ...] | list[str], *, timeout_ms: int = 2500) -> str | None:
    for selector in selectors:
        try:
            locator = page.locator(selector)
            count = await locator.count()
            if count <= 0:
                continue
            target = locator.last if count > 1 else locator.first
            await target.click(timeout=timeout_ms)
            await page.wait_for_timeout(450)
            return selector
        except Exception:
            continue
    return None


async def upload_file_to_current_chat(page: Page, file_path: Path) -> tuple[bool, str | None]:
    from wa_selectors import (
        WHATSAPP_ATTACH_BUTTON_SELECTORS,
        WHATSAPP_ATTACH_DOCUMENT_SELECTORS,
        WHATSAPP_ATTACH_MEDIA_SELECTORS,
    )

    menu_selectors = (
        list(WHATSAPP_ATTACH_MEDIA_SELECTORS) + list(WHATSAPP_ATTACH_DOCUMENT_SELECTORS)
        if attachment_prefers_media_menu(file_path)
        else list(WHATSAPP_ATTACH_DOCUMENT_SELECTORS) + list(WHATSAPP_ATTACH_MEDIA_SELECTORS)
    )

    for attach_selector in WHATSAPP_ATTACH_BUTTON_SELECTORS:
        try:
            attach = page.locator(attach_selector)
            if await attach.count() <= 0:
                continue

            attach_target = attach.last
            try:
                async with page.expect_file_chooser(timeout=7000) as fc_info:
                    await attach_target.click(timeout=3000)
                    await page.wait_for_timeout(350)
                    clicked_menu = await _click_first_visible(page, menu_selectors)
                    if not clicked_menu:
                        if not await _set_files_on_input(page, file_path):
                            raise RuntimeError("menu_not_found")
                file_chooser = await fc_info.value
                await file_chooser.set_files(str(file_path))
                await page.wait_for_timeout(1200)
                return True, f"file_chooser:{attach_selector}"
            except Exception:
                await attach_target.click(timeout=2500)
                await page.wait_for_timeout(350)
                if await _click_first_visible(page, menu_selectors):
                    if await _set_files_on_input(page, file_path):
                        return True, f"menu_input:{attach_selector}"
                if await _set_files_on_input(page, file_path):
                    return True, f"input_only:{attach_selector}"
        except Exception:
            continue

    if await _set_files_on_input(page, file_path):
        return True, "fallback_input"
    return False, "Não encontrei o botão de anexar ou o seletor de arquivo do WhatsApp Web."


FIND_ATTACHMENT_CAPTION_JS = r"""
() => {
    const visible = (el) => {
        const r = el.getBoundingClientRect();
        const s = window.getComputedStyle(el);
        return r.width > 8 && r.height > 8 &&
               s.display !== "none" && s.visibility !== "hidden" &&
               r.bottom >= 0 && r.right >= 0 &&
               r.top <= window.innerHeight && r.left <= window.innerWidth;
    };

    const selectors = [
        "div[data-testid='media-caption-input-container'] div[contenteditable='true']",
        "div[data-testid='media-caption-input']",
        "[aria-label*='Adicionar legenda' i][contenteditable='true']",
        "[aria-label*='Add a caption' i][contenteditable='true']",
        "[aria-label*='legenda' i][contenteditable='true']",
        "[aria-label*='caption' i][contenteditable='true']",
        "div[role='dialog'] footer div[contenteditable='true']",
        "div[role='dialog'] div[contenteditable='true'][data-tab]",
    ];

    for (const selector of selectors) {
        const elements = Array.from(document.querySelectorAll(selector)).filter(visible);
        if (!elements.length) continue;
        const el = elements[elements.length - 1];
        const r = el.getBoundingClientRect();
        return { x: r.x, y: r.y, width: r.width, height: r.height, selector };
    }

    const footer = document.querySelector("footer");
    const overlayEditables = Array.from(document.querySelectorAll("div[contenteditable='true']"))
        .filter(visible)
        .filter((el) => {
            if (footer && footer.contains(el)) return false;
            return !!el.closest(
                "[role='dialog'], [data-testid='media-viewer'], [data-testid='media-caption-input-container']"
            );
        });
    if (overlayEditables.length) {
        const el = overlayEditables[overlayEditables.length - 1];
        const r = el.getBoundingClientRect();
        return { x: r.x, y: r.y, width: r.width, height: r.height, selector: "overlay-contenteditable" };
    }
    return null;
}
"""

GET_ATTACHMENT_CAPTION_TEXT_JS = r"""
() => {
    const visible = (el) => {
        const r = el.getBoundingClientRect();
        const s = window.getComputedStyle(el);
        return r.width > 8 && r.height > 8 && s.display !== "none" && s.visibility !== "hidden";
    };

    const selectors = [
        "div[data-testid='media-caption-input-container'] div[contenteditable='true']",
        "div[data-testid='media-caption-input']",
        "[aria-label*='legenda' i][contenteditable='true']",
        "[aria-label*='caption' i][contenteditable='true']",
        "div[role='dialog'] footer div[contenteditable='true']",
    ];
    for (const selector of selectors) {
        const elements = Array.from(document.querySelectorAll(selector)).filter(visible);
        if (!elements.length) continue;
        const el = elements[elements.length - 1];
        return el.innerText || el.textContent || "";
    }
    return "";
}
"""

WAIT_MEDIA_PREVIEW_JS = r"""
() => {
    const visible = (el) => {
        const r = el.getBoundingClientRect();
        const s = window.getComputedStyle(el);
        return r.width > 0 && r.height > 0 && s.display !== "none" && s.visibility !== "hidden";
    };

    const hasSend = Array.from(document.querySelectorAll(
        "[data-testid='send'], div[role='dialog'] span[data-icon='send'], span[data-testid='send']"
    )).some(visible);
    const hasPreview = Array.from(document.querySelectorAll(
        "div[data-testid='media-caption-input-container'], div[data-testid='media-viewer'], " +
        "canvas, video, img[src^='blob:'], img[src^='data:']"
    )).some(visible);
    const captionEl = document.querySelector(
        "div[data-testid='media-caption-input-container'] div[contenteditable='true'], " +
        "[aria-label*='legenda' i][contenteditable='true'], [aria-label*='caption' i][contenteditable='true']"
    );
    return {
        ready: hasSend || hasPreview,
        has_caption_box: !!(captionEl && visible(captionEl)),
        has_send: hasSend,
        has_preview: hasPreview,
    };
}
"""

SET_ATTACHMENT_CAPTION_JS = r"""
(text) => {
    const visible = (el) => {
        const r = el.getBoundingClientRect();
        const s = window.getComputedStyle(el);
        return r.width > 8 && r.height > 8 && s.display !== "none" && s.visibility !== "hidden";
    };

    const selectors = [
        "div[data-testid='media-caption-input-container'] div[contenteditable='true']",
        "div[data-testid='media-caption-input']",
        "[aria-label*='legenda' i][contenteditable='true']",
        "[aria-label*='caption' i][contenteditable='true']",
        "div[role='dialog'] footer div[contenteditable='true']",
    ];

    let el = null;
    for (const selector of selectors) {
        const elements = Array.from(document.querySelectorAll(selector)).filter(visible);
        if (elements.length) {
            el = elements[elements.length - 1];
            break;
        }
    }
    if (!el) return { ok: false, error: "caption_element_not_found" };

    el.focus();
    el.textContent = text;
    el.innerText = text;
    el.dispatchEvent(new InputEvent("input", { bubbles: true }));
    el.dispatchEvent(new Event("change", { bubbles: true }));
    return { ok: true, text: el.innerText || el.textContent || "" };
}
"""


async def wait_for_media_preview_ready(page: Page, *, timeout_ms: int = 20000) -> Dict[str, Any]:
    deadline = time.monotonic() + (timeout_ms / 1000)
    last_state: Dict[str, Any] = {}
    while time.monotonic() < deadline:
        try:
            last_state = await page.evaluate(WAIT_MEDIA_PREVIEW_JS)
            if last_state.get("ready"):
                return {"ok": True, **last_state}
        except Exception:
            pass
        await page.wait_for_timeout(400)
    return {
        "ok": False,
        "error": "A pré-visualização do anexo não apareceu a tempo.",
        **last_state,
    }


async def click_attachment_caption_box(page: Page) -> bool:
    box = await page.evaluate(FIND_ATTACHMENT_CAPTION_JS)
    if not box:
        return False
    await page.mouse.click(
        box["x"] + max(12, box["width"] / 2),
        box["y"] + max(12, box["height"] / 2),
    )
    await page.wait_for_timeout(350)
    return True


async def get_attachment_caption_text(page: Page) -> str:
    try:
        return str(await page.evaluate(GET_ATTACHMENT_CAPTION_TEXT_JS) or "")
    except Exception:
        return ""


def caption_matches_expected(caption_text: str, expected: str) -> bool:
    draft = normalize_for_compare(caption_text)
    if not expected:
        return True
    return draft == expected or expected in draft


async def fill_attachment_caption(page: Page, caption: str) -> Dict[str, Any]:
    text = (caption or "").strip()
    if not text:
        return {"ok": True, "method": "no_caption", "verified": True}

    expected = normalize_for_compare(text)
    preview = await wait_for_media_preview_ready(page)
    if not preview.get("ok"):
        return {
            "ok": False,
            "verified": False,
            "supports_caption": False,
            "error": preview.get("error") or "Preview indisponível.",
        }

    supports_caption = bool(preview.get("has_caption_box"))

    if await click_attachment_caption_box(page):
        try:
            await page.keyboard.press("Control+A")
            await page.keyboard.press("Backspace")
            await page.wait_for_timeout(120)
            await page.keyboard.insert_text(text)
            await page.wait_for_timeout(500)
            draft = normalize_for_compare(await get_attachment_caption_text(page))
            if caption_matches_expected(draft, expected):
                return {
                    "ok": True,
                    "verified": True,
                    "method": "caption.insert_text",
                    "draft_text": draft,
                    "supports_caption": True,
                }
        except Exception:
            pass

        try:
            await click_attachment_caption_box(page)
            await type_multiline_message(page, text)
            await page.wait_for_timeout(500)
            draft = normalize_for_compare(await get_attachment_caption_text(page))
            if caption_matches_expected(draft, expected):
                return {
                    "ok": True,
                    "verified": True,
                    "method": "caption.type",
                    "draft_text": draft,
                    "supports_caption": True,
                }
        except Exception:
            pass

    try:
        set_result = await page.evaluate(SET_ATTACHMENT_CAPTION_JS, text)
        await page.wait_for_timeout(500)
        draft = normalize_for_compare(await get_attachment_caption_text(page))
        if set_result.get("ok") and caption_matches_expected(draft, expected):
            return {
                "ok": True,
                "verified": True,
                "method": "caption.js_set",
                "draft_text": draft,
                "supports_caption": True,
            }
    except Exception:
        pass

    caption_selectors = [
        "div[data-testid='media-caption-input-container'] div[contenteditable='true']",
        "div[role='dialog'] footer div[contenteditable='true']",
        "[aria-label*='legenda' i][contenteditable='true']",
        "[aria-label*='caption' i][contenteditable='true']",
    ]
    for selector in caption_selectors:
        try:
            locator = page.locator(selector).last
            if await locator.count() <= 0:
                continue
            await locator.click(timeout=2500)
            await locator.fill(text, timeout=4000)
            await page.wait_for_timeout(500)
            draft = normalize_for_compare(await get_attachment_caption_text(page))
            if caption_matches_expected(draft, expected):
                return {
                    "ok": True,
                    "verified": True,
                    "method": f"caption.fill:{selector}",
                    "draft_text": draft,
                    "supports_caption": True,
                }
        except Exception:
            continue

    draft_after = normalize_for_compare(await get_attachment_caption_text(page))
    return {
        "ok": False,
        "verified": False,
        "supports_caption": supports_caption,
        "error": "Não consegui confirmar a legenda na pré-visualização do anexo.",
        "draft_text": draft_after,
        "expected": expected,
    }


ATTACHMENT_SENT_JS = """
() => {
    const outs = Array.from(document.querySelectorAll("div.message-out"));
    if (!outs.length) return { ok: false, reason: "no_outgoing" };
    const last = outs[outs.length - 1];
    const hasMedia = !!last.querySelector(
        "img, video, audio, [data-icon='document'], [data-icon='audio'], [data-icon='ptt']"
    );
    const pending = !!last.querySelector(
        "[data-icon='msg-time'], [data-icon='msg-clock'], [data-icon='media-time']"
    );
    return { ok: hasMedia && !pending, hasMedia, pending };
}
"""


async def wait_for_attachment_sent(page: Page, *, timeout_ms: int = 35000) -> Dict[str, Any]:
    deadline = time.monotonic() + (timeout_ms / 1000)
    while time.monotonic() < deadline:
        try:
            state = await page.evaluate(ATTACHMENT_SENT_JS)
            if state.get("ok"):
                return {
                    "ok": True,
                    "verification": "outgoing_media_visible",
                    "media_state": state,
                }
        except Exception:
            pass
        await page.wait_for_timeout(800)
    return {
        "ok": False,
        "error": "O anexo não foi confirmado no chat após o envio.",
        "verification": "attachment_not_verified",
    }


async def click_preview_send_button(page: Page) -> str | None:
    preview_selectors = [
        "[data-testid='send']",
        "div[role='dialog'] span[data-icon='send']",
        "div[role='dialog'] button[aria-label*='Enviar' i]",
        "div[role='dialog'] button[aria-label*='Send' i]",
        "span[data-testid='send']",
    ]
    for selector in preview_selectors:
        try:
            locator = page.locator(selector)
            if await locator.count() <= 0:
                continue
            await locator.last.click(timeout=3500)
            await page.wait_for_timeout(700)
            return selector
        except Exception:
            continue
    return None


async def send_attachment_to_current_chat(
    page: Page,
    file_path: Path,
    caption: str = "",
) -> Dict[str, Any]:
    if not file_path.is_file():
        return {"ok": False, "error": f"Arquivo não encontrado: {file_path}"}

    ready = await wait_for_chat_send_ready(page, timeout_ms=30000)
    if not ready.get("ok"):
        return {
            "ok": False,
            "error": ready.get("error") or "A conversa não está pronta para envio.",
            "chat_state": ready.get("state"),
        }

    uploaded, upload_method = await upload_file_to_current_chat(page, file_path)
    if not uploaded:
        return {"ok": False, "error": upload_method or "Falha ao selecionar o arquivo para envio."}

    caption_text = (caption or "").strip()
    caption_result: Dict[str, Any] = {"ok": True, "verified": True, "method": "no_caption"}
    caption_verified = not caption_text

    if caption_text:
        caption_result = await fill_attachment_caption(page, caption_text)
        caption_verified = bool(caption_result.get("verified"))
        if not caption_result.get("ok") and not caption_result.get("supports_caption", True):
            print(
                "Legenda indisponível neste tipo de anexo; "
                "o texto será enviado em mensagem separada após o arquivo."
            )
        elif not caption_verified:
            print(
                "Aviso: legenda não confirmada na pré-visualização; "
                "tentando enviar o anexo e depois a mensagem separada."
            )

    preview_send = await click_preview_send_button(page)
    send_method = preview_send or await click_send_button_or_press_enter(page)
    verified = await wait_for_attachment_sent(page)

    if not verified.get("ok"):
        verified.update({
            "send_method": send_method,
            "upload_method": upload_method,
            "attachment_name": file_path.name,
            "caption_result": caption_result,
        })
        return verified

    result: Dict[str, Any] = {
        "ok": True,
        "verified": True,
        "verification": verified.get("verification"),
        "send_method": send_method,
        "upload_method": upload_method,
        "attachment_name": file_path.name,
        "caption_chars": len(caption_text),
        "caption_method": caption_result.get("method"),
        "caption_verified": caption_verified,
        "sent_at": now_iso(),
    }

    if caption_text and not caption_verified:
        await page.wait_for_timeout(1800)
        text_result = await send_text_to_current_chat(page, caption_text)
        result["caption_fallback"] = "separate_text_message"
        result["text_follow_up"] = text_result
        if not text_result.get("ok"):
            result["ok"] = False
            result["error"] = (
                "Anexo enviado, mas a mensagem de texto não foi confirmada: "
                f"{text_result.get('error') or 'erro desconhecido'}"
            )

    return result


READ_MESSAGES_JS = """
() => {
    const normalize = (txt) => (txt || "")
        .replace(/\\u200e/g, "")
        .replace(/\\u200f/g, "")
        .replace(/\\s+/g, " ")
        .trim();

    const visible = (el) => {
        const r = el.getBoundingClientRect();
        const s = window.getComputedStyle(el);
        return r.width > 0 &&
               r.height > 0 &&
               s.visibility !== "hidden" &&
               s.display !== "none" &&
               r.bottom >= 0 &&
               r.right >= 0 &&
               r.top <= window.innerHeight &&
               r.left <= window.innerWidth;
    };

    const parsePrePlainText = (pre) => {
        const value = normalize(pre);
        const match = value.match(/^\\[(.*?)\\]\\s(.*?):\\s*$/);
        if (match) {
            return { timestamp_text: match[1], sender: match[2] };
        }
        return { timestamp_text: null, sender: null };
    };

    const directionOf = (el) => {
        if (el.closest(".message-in")) return "recebida";
        if (el.closest(".message-out")) return "enviada";
        return "indefinida";
    };

    const getText = (el) => {
        const nodes = Array.from(el.querySelectorAll(
            "span.selectable-text.copyable-text, span.selectable-text, span[dir='ltr'], span[dir='auto'], span[data-testid='selectable-text']"
        ));

        const texts = nodes
            .map(n => normalize(n.innerText || n.textContent || ""))
            .filter(Boolean);

        if (texts.length) return normalize(texts.join("\\n"));

        return normalize(el.innerText || el.textContent || "");
    };

    const primary = Array.from(document.querySelectorAll(
        "div.copyable-text[data-pre-plain-text], div[data-pre-plain-text], [data-testid='msg-container']"
    ));

    const fallback = Array.from(document.querySelectorAll(
        "div.message-in, div.message-out, [data-testid='conversation-panel-messages'] div[role='row']"
    ));

    const all = [...primary, ...fallback];
    const seen = new Set();
    const messages = [];

    for (const el of all) {
        if (!visible(el)) continue;

        const r = el.getBoundingClientRect();
        const centerX = r.x + r.width / 2;

        // Evita lista lateral de conversas.
        if (centerX < window.innerWidth * 0.25) continue;

        const pre =
            el.getAttribute("data-pre-plain-text") ||
            el.querySelector("[data-pre-plain-text]")?.getAttribute("data-pre-plain-text") ||
            "";

        const meta = parsePrePlainText(pre);
        const text = getText(el);

        if (!text) continue;
        if (text.length > 5000) continue;

        const direction = directionOf(el);

        const key = [
            direction,
            meta.timestamp_text || "",
            meta.sender || "",
            text,
            Math.round(r.y)
        ].join("|");

        if (seen.has(key)) continue;
        seen.add(key);

        messages.push({
            direction,
            sender: meta.sender,
            timestamp_text: meta.timestamp_text,
            text,
            x: Math.round(r.x),
            y: Math.round(r.y),
            width: Math.round(r.width),
            height: Math.round(r.height)
        });
    }

    return messages.sort((a, b) => a.y - b.y);
}
"""


SCROLL_CHAT_BOTTOM_JS = """
() => {
    const hasMessages = (el) => {
        try {
            return el.querySelectorAll(
                "div.copyable-text[data-pre-plain-text], div[data-pre-plain-text], div.message-in, div.message-out, [data-testid='msg-container']"
            ).length > 0;
        } catch {
            return false;
        }
    };

    const candidates = Array.from(document.querySelectorAll("div"))
        .filter(el => {
            const r = el.getBoundingClientRect();
            const s = window.getComputedStyle(el);
            return r.width > window.innerWidth * 0.35 &&
                   r.height > window.innerHeight * 0.30 &&
                   r.x > window.innerWidth * 0.20 &&
                   el.scrollHeight > el.clientHeight + 20 &&
                   s.overflowY !== "hidden" &&
                   hasMessages(el);
        })
        .sort((a, b) => {
            const ar = a.getBoundingClientRect();
            const br = b.getBoundingClientRect();
            return (br.width * br.height) - (ar.width * ar.height);
        });

    const panel = candidates[0];
    if (!panel) {
        return { ok: false, reason: "painel_do_chat_nao_encontrado" };
    }

    panel.scrollTop = panel.scrollHeight;
    return { ok: true, scrollTop: panel.scrollTop, scrollHeight: panel.scrollHeight };
}
"""


SCROLL_CHAT_UP_JS = """
() => {
    const hasMessages = (el) => {
        try {
            return el.querySelectorAll(
                "div.copyable-text[data-pre-plain-text], div[data-pre-plain-text], div.message-in, div.message-out"
            ).length > 0;
        } catch {
            return false;
        }
    };

    const candidates = Array.from(document.querySelectorAll("div"))
        .filter(el => {
            const r = el.getBoundingClientRect();
            const s = window.getComputedStyle(el);
            return r.width > window.innerWidth * 0.35 &&
                   r.height > window.innerHeight * 0.30 &&
                   r.x > window.innerWidth * 0.20 &&
                   el.scrollHeight > el.clientHeight + 100 &&
                   s.overflowY !== "hidden" &&
                   hasMessages(el);
        })
        .sort((a, b) => {
            const ar = a.getBoundingClientRect();
            const br = b.getBoundingClientRect();
            return (br.width * br.height) - (ar.width * ar.height);
        });

    const panel = candidates[0];

    if (!panel) {
        window.scrollBy(0, -800);
        return { ok: false, reason: "painel_do_chat_nao_encontrado" };
    }

    const before = panel.scrollTop;
    panel.scrollTop = Math.max(0, panel.scrollTop - 900);

    return {
        ok: true,
        before,
        after: panel.scrollTop,
        scrollHeight: panel.scrollHeight,
        clientHeight: panel.clientHeight
    };
}
"""


def message_hash(target_id: str, message: Dict[str, Any]) -> str:
    raw = json.dumps(
        {
            "target_id": target_id,
            "direction": message.get("direction"),
            "sender": message.get("sender"),
            "timestamp_text": message.get("timestamp_text"),
            "text": message.get("text"),
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


async def read_current_messages(page: Page, target: Target) -> List[Dict[str, Any]]:
    messages: List[Dict[str, Any]] = await page.evaluate(READ_MESSAGES_JS)
    captured_at = now_iso()
    enriched = []

    for msg in messages:
        item = {
            "captured_at": captured_at,
            "target_id": target.id,
            "target_type": target.type,
            "target_name": target.name,
            "target_phone": target.phone,
            "direction": msg.get("direction"),
            "sender": msg.get("sender"),
            "timestamp_text": msg.get("timestamp_text"),
            "text": msg.get("text"),
            "position": {
                "x": msg.get("x"),
                "y": msg.get("y"),
                "width": msg.get("width"),
                "height": msg.get("height"),
            },
        }
        item["hash"] = message_hash(target.id, item)
        enriched.append(item)

    return enriched


async def collect_messages_for_target(
    page: Page,
    target: Target,
    scrolls: int,
    delay: float,
) -> List[Dict[str, Any]]:
    all_by_hash: Dict[str, Dict[str, Any]] = {}

    for i in range(max(1, scrolls)):
        messages = await read_current_messages(page, target)
        for msg in messages:
            all_by_hash[msg["hash"]] = msg

        print(f"  Rolagem {i + 1}/{scrolls}: {len(all_by_hash)} mensagens únicas capturadas")

        if i < scrolls - 1:
            await page.evaluate(SCROLL_CHAT_UP_JS)
            await page.wait_for_timeout(int(delay * 1000))

    return list(all_by_hash.values())


def append_jsonl(path: Path, messages: List[Dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        for msg in messages:
            f.write(json.dumps(msg, ensure_ascii=False) + "\n")


def write_latest_json(path: Path, messages: List[Dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(messages, ensure_ascii=False, indent=2), encoding="utf-8")


def write_csv(path: Path, messages: List[Dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = [
        "captured_at",
        "target_id",
        "target_type",
        "target_name",
        "target_phone",
        "direction",
        "sender",
        "timestamp_text",
        "text",
        "hash",
    ]
    with path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        for msg in messages:
            writer.writerow({k: msg.get(k) for k in fields})


def target_display_name(target: Target) -> str:
    return target.name or target.phone or target.id


def build_send_record(
    target: Target,
    message: str,
    result: Dict[str, Any],
    *,
    dry_run: bool,
    attachment: str | None = None,
) -> Dict[str, Any]:
    return {
        "created_at": now_iso(),
        "target_id": target.id,
        "target_type": target.type,
        "target_name": target.name,
        "target_phone": target.phone,
        "message": message,
        "message_chars": len(message or ""),
        "attachment": attachment,
        "attachment_name": Path(attachment).name if attachment else result.get("attachment_name"),
        "dry_run": dry_run,
        "ok": bool(result.get("ok")),
        "verified": bool(result.get("verified") or result.get("verification") in {"outgoing_message_visible", "outgoing_message_visible_and_not_pending"}),
        "verification": result.get("verification"),
        "send_method": result.get("send_method"),
        "filled_method": result.get("filled_method"),
        "matches_before": result.get("matches_before"),
        "matches_after": result.get("matches_after"),
        "delivery_status_icon": result.get("delivery_status_icon"),
        "delivery_status_label": result.get("delivery_status_label"),
        "last_status": result.get("last_status"),
        "sent_at": result.get("sent_at"),
        "error": result.get("error"),
        "draft_text_after": result.get("draft_text_after"),
    }


def write_send_latest(path: Path, records: List[Dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8")


EXTRACT_WHATSAPP_CHATS_JS = r"""
async (extractMode) => {
    const wantGroups = (extractMode || "groups") === "groups";
    const normalize = (txt) => String(txt || "")
        .replace(/\u200e/g, "")
        .replace(/\u200f/g, "")
        .replace(/\s+/g, " ")
        .trim();

    const toPlain = (value, depth = 0) => {
        if (value == null) return value;
        if (depth > 2) return undefined;
        if (["string", "number", "boolean"].includes(typeof value)) return value;
        if (Array.isArray(value)) return value.slice(0, 20).map(v => toPlain(v, depth + 1));
        if (typeof value === "object") {
            const out = {};
            for (const key of Object.keys(value).slice(0, 60)) {
                if (key.startsWith("_") && !["_serialized", "__x_name", "__x_formattedTitle", "__x_pushname", "__x_subject", "__x_id", "__x_isGroup", "__x_groupMetadata", "__x_archive", "__x_unreadCount", "__x_t"].includes(key)) continue;
                try {
                    const item = value[key];
                    if (typeof item !== "function") out[key] = toPlain(item, depth + 1);
                } catch {}
            }
            return out;
        }
        return undefined;
    };

    const objectValuesSafe = (obj) => {
        try {
            if (!obj) return [];
            if (Array.isArray(obj)) return obj;
            if (obj instanceof Map) return Array.from(obj.values());
            if (typeof obj.toArray === "function") return obj.toArray();
            if (Array.isArray(obj.models)) return obj.models;
            if (obj.models instanceof Map) return Array.from(obj.models.values());
            if (obj._models instanceof Map) return Array.from(obj._models.values());
            if (Array.isArray(obj._models)) return obj._models;
            if (obj._models && typeof obj._models === "object") return Object.values(obj._models);
            if (obj.__x_models instanceof Map) return Array.from(obj.__x_models.values());
            if (Array.isArray(obj.__x_models)) return obj.__x_models;
            return [];
        } catch {
            return [];
        }
    };

    const readId = (chat) => {
        const id = chat?.id || chat?.__x_id || chat?.wid || chat?.__x_wid || chat?.jid || chat?.__x_jid;
        if (typeof id === "string") return id;
        return normalize(
            id?._serialized || id?.serialized || id?.toString?.() ||
            (id?.user && id?.server ? `${id.user}@${id.server}` : "") ||
            chat?._serialized || chat?.__x__serialized || ""
        );
    };

    const isGroupChat = (chat) => {
        const id = readId(chat).toLowerCase();
        return Boolean(
            id.includes("@g.us") ||
            chat?.isGroup === true ||
            chat?.__x_isGroup === true ||
            chat?.id?.server === "g.us" ||
            chat?.__x_id?.server === "g.us" ||
            chat?.groupMetadata ||
            chat?.__x_groupMetadata
        );
    };

    const phoneFromWhatsappId = (whatsappId) => {
        const id = String(whatsappId || "").toLowerCase();
        const user = id.split("@")[0] || "";
        const digits = user.replace(/\D/g, "");
        return digits.length >= 8 ? digits : null;
    };

    const isPrivateChat = (chat) => {
        if (isGroupChat(chat)) return false;
        const id = readId(chat).toLowerCase();
        if (!id) return false;
        if (id.includes("@broadcast") || id.includes("@newsletter") || id.includes("@status")) return false;
        if (id.includes("@g.us") || id.includes("@lid")) return false;
        return id.includes("@c.us") || id.includes("@s.whatsapp.net") || phoneFromWhatsappId(id) != null;
    };

    const matchesMode = (chat) => (wantGroups ? isGroupChat(chat) : isPrivateChat(chat));

    const readName = (chat) => {
        const groupMeta = chat?.groupMetadata || chat?.__x_groupMetadata || {};
        return normalize(
            chat?.formattedTitle || chat?.__x_formattedTitle ||
            chat?.name || chat?.__x_name ||
            chat?.contact?.formattedName || chat?.contact?.name ||
            chat?.__x_contact?.formattedName || chat?.__x_contact?.name ||
            groupMeta?.subject || groupMeta?.__x_subject ||
            chat?.subject || chat?.__x_subject ||
            chat?.pushname || chat?.__x_pushname ||
            readId(chat)
        );
    };

    const readTimestamp = (chat) => {
        const value = chat?.t || chat?.__x_t || chat?.timestamp || chat?.__x_timestamp || chat?.lastReceivedKey?.t || null;
        if (!value) return null;
        const n = Number(value);
        if (!Number.isFinite(n)) return value;
        const ms = n < 10_000_000_000 ? n * 1000 : n;
        try { return new Date(ms).toISOString(); } catch { return value; }
    };

    const readBool = (...values) => {
        for (const v of values) {
            if (typeof v === "boolean") return v;
            if (typeof v === "number") return Boolean(v);
        }
        return false;
    };

    const makeEntry = (chat, source) => {
        const whatsappId = readId(chat);
        const name = readName(chat);
        const base = {
            whatsapp_id: whatsappId || null,
            name: name || whatsappId || null,
            source,
            unread_count: Number(chat?.unreadCount ?? chat?.__x_unreadCount ?? chat?.unreadMsgs ?? 0) || 0,
            archived: readBool(chat?.archive, chat?.isArchived, chat?.__x_archive),
            pinned: readBool(chat?.pin, chat?.isPinned, chat?.__x_pin),
            muted: readBool(chat?.mute, chat?.isMuted, chat?.__x_mute),
            last_message_at: readTimestamp(chat),
            raw_preview: toPlain(chat, 0)
        };
        if (wantGroups) {
            return { ...base, type: "group" };
        }
        return { ...base, type: "contact", phone: phoneFromWhatsappId(whatsappId) };
    };

    const seenCollections = new WeakSet();
    const seenObjects = new WeakSet();
    const collections = [];
    const diagnostics = {
        store_present: Boolean(window.Store),
        wpp_present: Boolean(window.WPP),
        webpack_cache_modules: 0,
        collections_found: 0,
        indexeddb_entries_found: 0,
        dom_chat_titles_found: 0,
        errors: []
    };

    const addCollection = (value, label) => {
        if (!value || typeof value !== "object") return;
        if (seenCollections.has(value)) return;
        const items = objectValuesSafe(value);
        if (!items.length) return;
        const sample = items.slice(0, 80);
        const modeCount = sample.filter(matchesMode).length;
        const idLikeCount = sample.filter(item => readId(item)).length;
        if (modeCount > 0 || idLikeCount >= Math.min(5, sample.length)) {
            seenCollections.add(value);
            collections.push({label, value, items});
        }
    };

    const inspectObject = (obj, label, depth = 0) => {
        if (!obj || typeof obj !== "object" || depth > 2) return;
        if (seenObjects.has(obj)) return;
        seenObjects.add(obj);

        addCollection(obj, label);

        const keys = [];
        try { keys.push(...Object.keys(obj).slice(0, 80)); } catch {}
        for (const key of keys) {
            let value;
            try { value = obj[key]; } catch { continue; }
            if (!value || typeof value === "function") continue;
            if (typeof value === "object") {
                if (key.toLowerCase().includes("chat") || key.toLowerCase().includes("collection") || key.toLowerCase().includes("store") || key.toLowerCase().includes("contact")) {
                    addCollection(value, `${label}.${key}`);
                }
                if (depth < 1) inspectObject(value, `${label}.${key}`, depth + 1);
            }
        }
    };

    const getWebpackRequire = () => {
        const chunk = window.webpackChunkwhatsapp_web_client || window.webpackChunkbuild;
        if (!chunk || !chunk.push) return null;
        let req = null;
        try {
            const chunkId = Math.random().toString(36).slice(2);
            chunk.push([[chunkId], {}, (__webpack_require__) => { req = __webpack_require__; }]);
        } catch (e) {
            diagnostics.errors.push(`webpack_require: ${e?.message || e}`);
        }
        return req;
    };

    // 1) Tenta fontes internas carregadas na própria página do WhatsApp Web.
    try {
        if (window.Store) inspectObject(window.Store, "window.Store", 0);
        if (window.WPP) inspectObject(window.WPP, "window.WPP", 0);
        const req = getWebpackRequire();
        if (req?.c) {
            const modules = Object.values(req.c).map(m => m?.exports).filter(Boolean);
            diagnostics.webpack_cache_modules = modules.length;
            for (let i = 0; i < modules.length; i++) {
                const exp = modules[i];
                inspectObject(exp, `webpack[${i}]`, 0);
                if (exp?.default && typeof exp.default === "object") inspectObject(exp.default, `webpack[${i}].default`, 0);
            }
        }
    } catch (e) {
        diagnostics.errors.push(`internal_store_scan: ${e?.message || e}`);
    }

    const entriesByKey = new Map();
    for (const collection of collections) {
        for (const item of collection.items) {
            if (!matchesMode(item)) continue;
            const entry = makeEntry(item, collection.label);
            if (!wantGroups && !entry.phone) continue;
            const key = wantGroups ? (entry.whatsapp_id || entry.name) : (entry.phone || entry.whatsapp_id);
            if (!key) continue;
            if (!entriesByKey.has(key)) entriesByKey.set(key, entry);
        }
    }
    diagnostics.collections_found = collections.length;

    // 2) Fallback: varre IndexedDB da sessão do WhatsApp Web.
    const scanIndexedDb = async () => {
        const marker = wantGroups ? "@g.us" : "@c.us";
        const idPattern = wantGroups
            ? /[0-9]{5,}@[a-z.]?g\.us|[0-9]{5,}@g\.us/g
            : /[0-9]{5,}@c\.us|[0-9]{5,}@s\.whatsapp\.net/g;
        if (!indexedDB?.databases) return [];
        const dbs = await indexedDB.databases();
        const out = [];
        const openDb = (name) => new Promise((resolve, reject) => {
            const req = indexedDB.open(name);
            req.onsuccess = () => resolve(req.result);
            req.onerror = () => reject(req.error);
        });
        const getAllLimited = (store, limit) => new Promise((resolve) => {
            const rows = [];
            try {
                const cursorReq = store.openCursor();
                cursorReq.onsuccess = (event) => {
                    const cursor = event.target.result;
                    if (!cursor || rows.length >= limit) return resolve(rows);
                    rows.push(cursor.value);
                    cursor.continue();
                };
                cursorReq.onerror = () => resolve(rows);
            } catch {
                resolve(rows);
            }
        });
        for (const dbInfo of dbs.slice(0, 25)) {
            if (!dbInfo?.name) continue;
            let db;
            try { db = await openDb(dbInfo.name); } catch { continue; }
            try {
                const storeNames = Array.from(db.objectStoreNames || []);
                for (const storeName of storeNames) {
                    const lname = String(storeName).toLowerCase();
                    const likely = lname.includes("chat") || lname.includes("group") || lname.includes("contact") || lname.includes("conversation") || lname.includes("model");
                    if (!likely) continue;
                    let tx, store;
                    try {
                        tx = db.transaction(storeName, "readonly");
                        store = tx.objectStore(storeName);
                    } catch { continue; }
                    const rows = await getAllLimited(store, 8000);
                    for (const row of rows) {
                        let text = "";
                        try { text = JSON.stringify(row); } catch { continue; }
                        if (!text.includes(marker) && !(wantGroups ? false : text.includes("@s.whatsapp.net"))) continue;
                        const idMatch = text.match(idPattern);
                        const ids = Array.from(new Set(idMatch || []));
                        for (const id of ids) {
                            const phone = phoneFromWhatsappId(id);
                            if (!wantGroups && !phone) continue;
                            out.push({
                                whatsapp_id: id,
                                phone: phone,
                                name: normalize(row?.name || row?.formattedTitle || row?.subject || row?.pushname || row?.__x_name || row?.__x_formattedTitle || row?.__x_subject || phone || id),
                                type: wantGroups ? "group" : "contact",
                                source: `indexedDB:${dbInfo.name}/${storeName}`,
                                unread_count: Number(row?.unreadCount ?? row?.__x_unreadCount ?? 0) || 0,
                                archived: readBool(row?.archive, row?.isArchived, row?.__x_archive),
                                pinned: readBool(row?.pin, row?.isPinned, row?.__x_pin),
                                muted: readBool(row?.mute, row?.isMuted, row?.__x_mute),
                                last_message_at: null,
                                raw_preview: toPlain(row, 0)
                            });
                        }
                    }
                }
            } finally {
                try { db.close(); } catch {}
            }
        }
        return out;
    };

    try {
        const idbEntries = await scanIndexedDb();
        diagnostics.indexeddb_entries_found = idbEntries.length;
        for (const entry of idbEntries) {
            const key = wantGroups ? (entry.whatsapp_id || entry.name) : (entry.phone || entry.whatsapp_id);
            if (key && !entriesByKey.has(key)) entriesByKey.set(key, entry);
        }
    } catch (e) {
        diagnostics.errors.push(`indexeddb_scan: ${e?.message || e}`);
    }

    const scanVisibleChatTitles = async () => {
        const pane = document.querySelector("#pane-side");
        if (!pane) return [];
        const seen = new Set();
        const results = [];
        const chatList = pane.querySelector("[data-testid='chat-list']");
        let scroller = chatList?.closest("[tabindex='-1']")
            || pane.querySelector("[tabindex='-1']")
            || chatList?.parentElement
            || pane;
        for (let round = 0; round < 40; round++) {
            for (const el of pane.querySelectorAll(
                "span[title], [data-testid='cell-frame-title'], [data-testid='cell-frame-container'] span[dir='auto']"
            )) {
                let title = normalize(el.getAttribute("title") || el.textContent || "");
                if (!title || title.length < 2) continue;
                const key = title.toLowerCase();
                if (seen.has(key)) continue;
                const r = el.getBoundingClientRect();
                if (r.width <= 0 || r.height <= 0 || r.x > window.innerWidth * 0.55) continue;
                seen.add(key);
                results.push({
                    whatsapp_id: null,
                    name: title,
                    type: wantGroups ? "group" : "contact",
                    source: "dom:pane-side",
                    unread_count: 0,
                    archived: false,
                    pinned: false,
                    muted: false,
                    last_message_at: null,
                    raw_preview: null
                });
            }
            const prevTop = scroller.scrollTop;
            scroller.scrollTop = scroller.scrollTop + Math.max(700, Math.floor((scroller.clientHeight || 720) * 0.9));
            await new Promise(r => setTimeout(r, 280));
            if (scroller.scrollTop === prevTop) break;
        }
        return results;
    };

    if (wantGroups) {
        try {
            const domEntries = await scanVisibleChatTitles();
            diagnostics.dom_chat_titles_found = domEntries.length;
            for (const entry of domEntries) {
                const nameKey = normalize(entry.name || "").toLowerCase();
                if (!nameKey) continue;
                const exists = Array.from(entriesByKey.values()).some(
                    item => normalize(item.name || "").toLowerCase() === nameKey
                );
                if (!exists) entriesByKey.set(`dom:${nameKey}`, entry);
            }
        } catch (e) {
            diagnostics.errors.push(`dom_pane_side_scan: ${e?.message || e}`);
        }
    }

    const entries = Array.from(entriesByKey.values())
        .filter(item => wantGroups ? (item.name || item.whatsapp_id) : item.phone)
        .sort((a, b) => String(a.name || a.phone || a.whatsapp_id).localeCompare(String(b.name || b.phone || b.whatsapp_id), "pt-BR"));

    if (wantGroups) {
        return {
            ok: entries.length > 0,
            generated_at_browser: new Date().toISOString(),
            total_groups: entries.length,
            groups: entries,
            diagnostics
        };
    }

    return {
        ok: entries.length > 0,
        generated_at_browser: new Date().toISOString(),
        total_contacts: entries.length,
        contacts: entries,
        diagnostics
    };
}
"""

# Compatibilidade com referências antigas ao script de grupos.
EXTRACT_WHATSAPP_GROUPS_JS = EXTRACT_WHATSAPP_CHATS_JS

SUPPLEMENT_GROUP_SCROLL_JS = r"""
async () => {
    const normalize = (txt) => String(txt || "").replace(/\s+/g, " ").trim();
    const pane = document.querySelector("#pane-side");
    if (!pane) return [];

    const seen = new Set();
    const results = [];
    const chatList = pane.querySelector("[data-testid='chat-list']");
    let scroller = chatList?.closest("[tabindex='-1']")
        || pane.querySelector("[tabindex='-1']")
        || chatList?.parentElement
        || pane;

    const collect = () => {
        for (const el of pane.querySelectorAll(
            "span[title], [data-testid='cell-frame-title'], [data-testid='cell-frame-container'] span[dir='auto']"
        )) {
            const title = normalize(el.getAttribute("title") || el.textContent || "");
            if (!title || title.length < 2) continue;
            const key = title.toLowerCase();
            if (seen.has(key)) continue;
            const r = el.getBoundingClientRect();
            if (r.width <= 0 || r.height <= 0 || r.x > window.innerWidth * 0.55) continue;
            seen.add(key);
            results.push(title);
        }
    };

    const tryClickFilter = (labels) => {
        for (const el of document.querySelectorAll("#pane-side button, #pane-side [role='tab'], #pane-side [role='button']")) {
            const text = normalize(el.textContent || el.getAttribute("aria-label") || "");
            const lower = text.toLowerCase();
            for (const label of labels) {
                const target = String(label || "").toLowerCase();
                if (lower === target || lower.includes(target)) {
                    try { el.click(); } catch {}
                    return true;
                }
            }
        }
        return false;
    };

    const scanCurrentFilter = async () => {
        try { scroller.scrollTop = 0; } catch {}
        collect();
        for (let round = 0; round < 80; round++) {
            collect();
            const prevTop = scroller.scrollTop;
            try {
                scroller.scrollTop = scroller.scrollTop + Math.max(700, Math.floor((scroller.clientHeight || 720) * 0.9));
                scroller.dispatchEvent(new WheelEvent("wheel", { deltaY: 900, bubbles: true }));
            } catch { break; }
            await new Promise(r => setTimeout(r, 220));
            if (scroller.scrollTop === prevTop) break;
        }
    };

    for (const labels of [["Grupos", "Groups"], ["Arquivadas", "Archived"], ["Todas", "All", "Todos"]]) {
        if (tryClickFilter(labels)) {
            await new Promise(r => setTimeout(r, 700));
            await scanCurrentFilter();
        }
    }

    return results;
}
"""

COLLECT_SEARCH_RESULT_TITLES_JS = r"""
(query) => {
    const normalize = (txt) => String(txt || "").replace(/\s+/g, " ").trim();
    const needle = String(query || "").trim().toLowerCase();
    const out = [];
    const seen = new Set();
    const pane = document.querySelector("#pane-side") || document.body;

    for (const el of pane.querySelectorAll("span[title], [data-testid='cell-frame-title'], [role='listitem'] span[dir='auto']")) {
        const title = normalize(el.getAttribute("title") || el.textContent || "");
        if (title.length < 3) continue;
        if (needle && !title.toLowerCase().includes(needle)) continue;
        const key = title.toLowerCase();
        if (seen.has(key)) continue;
        const r = el.getBoundingClientRect();
        if (r.width <= 0 || r.height <= 0 || r.x > window.innerWidth * 0.62) continue;
        seen.add(key);
        out.push(title);
    }
    return out;
}
"""

# Compatibilidade com referências antigas.
SUPPLEMENT_GROUP_TITLES_JS = COLLECT_SEARCH_RESULT_TITLES_JS

EXTRACT_INDEXEDDB_GROUPS_JS = r"""
async () => {
    const normalize = (txt) => String(txt || "").replace(/\s+/g, " ").trim();
    const readSerialized = (value) => {
        if (value == null) return "";
        if (typeof value === "string") return value;
        if (typeof value === "object") {
            return String(
                value._serialized || value.serialized ||
                (value.user && value.server ? `${value.user}@${value.server}` : "") ||
                value.id || ""
            );
        }
        return String(value);
    };
    const readRowId = (row) => readSerialized(row?.id || row?.__x_id || row?.chatId || row?.wid || row?.jid);
    const readRowName = (row) => normalize(
        row?.name || row?.__x_name ||
        row?.formattedTitle || row?.__x_formattedTitle ||
        row?.subject || row?.__x_subject ||
        row?.groupMetadata?.subject || row?.__x_groupMetadata?.subject ||
        row?.contact?.name || row?.__x_contact?.name || ""
    );
    const isGroupRow = (row) => {
        if (!row || typeof row !== "object") return false;
        const id = readRowId(row).toLowerCase();
        if (id.includes("@g.us")) return true;
        if (id.includes("@c.us") || id.includes("@s.whatsapp.net") || id.includes("@lid")) return false;
        return Boolean(
            (row?.isGroup === true || row?.__x_isGroup === true) &&
            readRowName(row)
        );
    };
    const isChatStore = (storeName) => {
        const lname = String(storeName || "").toLowerCase();
        if (lname.includes("message") || lname.includes("range") || lname.includes("contact")) return false;
        return lname.includes("chat") || lname.includes("group") || lname.includes("conversation");
    };
    const seen = new Set();
    const out = [];
    const pushGroup = (row, source) => {
        if (!isGroupRow(row)) return;
        const whatsappId = readRowId(row) || null;
        const name = readRowName(row) || whatsappId;
        const key = (whatsappId || name || "").toLowerCase();
        if (!key || seen.has(key)) return;
        seen.add(key);
        out.push({
            whatsapp_id: whatsappId || null,
            name,
            type: "group",
            source,
            unread_count: Number(row?.unreadCount ?? row?.__x_unreadCount ?? 0) || 0,
            archived: Boolean(row?.archive ?? row?.__x_archive ?? row?.isArchived ?? false),
            pinned: Boolean(row?.pin ?? row?.__x_pin ?? row?.isPinned ?? false),
            muted: Boolean(row?.mute ?? row?.__x_mute ?? row?.isMuted ?? false),
            last_message_at: null,
        });
    };
    const collectItems = (value) => {
        if (!value) return [];
        try {
            if (Array.isArray(value)) return value;
            if (value instanceof Map) return Array.from(value.values());
            if (typeof value.getModels === "function") return value.getModels();
            if (Array.isArray(value.models)) return value.models;
            if (value.models instanceof Map) return Array.from(value.models.values());
            if (value._models instanceof Map) return Array.from(value._models.values());
            if (Array.isArray(value._models)) return value._models;
        } catch {}
        return [];
    };
    const scanStoreObject = (root, label) => {
        if (!root || typeof root !== "object") return;
        for (const key of ["Chat", "GroupMetadata", "chat", "chats"]) {
            for (const item of collectItems(root[key])) pushGroup(item, `${label}.${key}`);
        }
        for (const item of collectItems(root)) pushGroup(item, label);
    };
    try {
        if (window.Store) scanStoreObject(window.Store, "store");
        if (window.WPP?.chat?.list) {
            for (const chat of window.WPP.chat.list()) pushGroup(chat, "wpp:chat.list");
        }
    } catch {}
    if (!indexedDB?.databases) return out;
    const openDb = (name) => new Promise((resolve, reject) => {
        const req = indexedDB.open(name);
        req.onsuccess = () => resolve(req.result);
        req.onerror = () => reject(req.error);
    });
    const walkStore = (store, limit) => new Promise((resolve) => {
        const rows = [];
        try {
            const req = store.openCursor();
            req.onsuccess = (ev) => {
                const cursor = ev.target.result;
                if (!cursor || rows.length >= limit) return resolve(rows);
                rows.push(cursor.value);
                cursor.continue();
            };
            req.onerror = () => resolve(rows);
        } catch { resolve(rows); }
    });
    const dbs = await indexedDB.databases();
    for (const dbInfo of dbs) {
        if (!dbInfo?.name) continue;
        let db;
        try { db = await openDb(dbInfo.name); } catch { continue; }
        try {
            for (const storeName of Array.from(db.objectStoreNames || [])) {
                if (!isChatStore(storeName)) continue;
                let store;
                try {
                    store = db.transaction(storeName, "readonly").objectStore(storeName);
                } catch { continue; }
                const rows = await walkStore(store, 25000);
                for (const row of rows) pushGroup(row, `indexedDB:${dbInfo.name}/${storeName}`);
            }
        } finally {
            try { db.close(); } catch {}
        }
    }
    return out;
}
"""


def default_group_search_prefixes() -> List[str]:
    """Prefixos para busca incremental de grupos ausentes no inventário."""
    letters = [chr(code) for code in range(ord("a"), ord("z") + 1)]
    digits = [str(d) for d in range(10)]
    return letters + digits


def _normalize_group_name(value: Optional[str]) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip().casefold()


_GARBAGE_GROUP_NAME_PATTERNS = (
    re.compile(r"mensagens?\s+n[aã]o\s+lidas", re.I),
    re.compile(r"^\d+\s+mensagens", re.I),
    re.compile(r"^type a message", re.I),
    re.compile(r"^digite uma mensagem", re.I),
    re.compile(r"^whatsapp$", re.I),
    re.compile(r"^arquivadas?$", re.I),
    re.compile(r"^grupos?$", re.I),
)


def is_plausible_group_name(name: Optional[str]) -> bool:
    """Rejeita rótulos de UI/ruído da busca lateral."""
    cleaned = re.sub(r"\s+", " ", str(name or "")).strip()
    if len(cleaned) < 2 or len(cleaned) > 180:
        return False
    for pattern in _GARBAGE_GROUP_NAME_PATTERNS:
        if pattern.search(cleaned):
            return False
    if cleaned.isdigit():
        return False
    return True


def _group_source_rank(source: Optional[str]) -> int:
    """Menor = melhor (preferir chat store / indexedDB sobre busca DOM)."""
    label = str(source or "").lower()
    if label.startswith("indexeddb:") and "/chat" in label:
        return 0
    if label.startswith("indexeddb:") and "@g.us" in label:
        return 1
    if label.startswith("store") or label.startswith("wpp:"):
        return 2
    if "indexeddb" in label:
        return 3
    if label.startswith("dom:"):
        return 4
    if label.startswith("search:discover"):
        return 5
    if label.startswith("search:prefix"):
        return 9
    return 6


def finalize_group_inventory(groups: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Remove ruído, deduplica por id/nome e prefere entradas com whatsapp_id."""
    merged: Dict[str, Dict[str, Any]] = {}

    def pick_name(entry: Dict[str, Any], wid: str) -> str:
        name = str(entry.get("name") or "").strip()
        if is_plausible_group_name(name):
            return name
        if wid:
            return wid.split("@")[0]
        return name

    def consider(entry: Dict[str, Any]) -> None:
        name = str(entry.get("name") or "").strip()
        wid = str(entry.get("whatsapp_id") or "").strip().lower()
        if wid and not wid.endswith("@g.us"):
            wid = ""
        if not is_plausible_group_name(name) and not wid:
            return

        key = wid or f"name:{_normalize_group_name(name)}"
        candidate = dict(entry)
        candidate["name"] = pick_name(candidate, wid)

        current = merged.get(key)
        if current is None:
            merged[key] = candidate
            return

        if _group_source_rank(str(candidate.get("source"))) < _group_source_rank(
            str(current.get("source"))
        ):
            winner, loser = candidate, current
        else:
            winner, loser = current, candidate

        if not winner.get("whatsapp_id") and loser.get("whatsapp_id"):
            winner["whatsapp_id"] = loser.get("whatsapp_id")
        loser_name = str(loser.get("name") or "")
        if is_plausible_group_name(loser_name) and len(loser_name) > len(str(winner.get("name") or "")):
            winner["name"] = loser_name
        merged[key] = winner

    for item in groups:
        consider(item)

    # Mescla entradas só com nome quando já existe grupo com o mesmo nome e @g.us.
    for key, entry in list(merged.items()):
        if str(key).startswith("name:"):
            name_key = _normalize_group_name(str(entry.get("name") or ""))
            for other_key, other in merged.items():
                if other_key == key:
                    continue
                if _normalize_group_name(str(other.get("name") or "")) != name_key:
                    continue
                if other.get("whatsapp_id"):
                    if not other.get("name") and is_plausible_group_name(entry.get("name")):
                        other["name"] = entry.get("name")
                    del merged[key]
                    break

    return sorted(
        merged.values(),
        key=lambda row: str(row.get("name") or row.get("whatsapp_id") or "").casefold(),
    )


def merge_group_entries(
    existing: List[Dict[str, Any]],
    supplement: List[Dict[str, Any]],
) -> tuple[List[Dict[str, Any]], int]:
    """Mescla entradas de grupos por nome/whatsapp_id."""
    by_key: Dict[str, Dict[str, Any]] = {}
    for item in existing:
        name_key = _normalize_group_name(str(item.get("name") or ""))
        wid = str(item.get("whatsapp_id") or "").strip().lower()
        key = wid or f"name:{name_key}"
        if key:
            by_key[key] = item

    added = 0
    for item in supplement:
        name = str(item.get("name") or "").strip()
        name_key = _normalize_group_name(name)
        if not name_key:
            continue
        wid = str(item.get("whatsapp_id") or "").strip().lower()
        key = wid or f"name:{name_key}"
        if key in by_key:
            current = by_key[key]
            if wid and not current.get("whatsapp_id"):
                current["whatsapp_id"] = item.get("whatsapp_id")
            if name and not current.get("name"):
                current["name"] = name
            continue
        by_key[key] = item
        added += 1

    merged = sorted(
        by_key.values(),
        key=lambda row: str(row.get("name") or row.get("whatsapp_id") or "").casefold(),
    )
    return merged, added


async def supplement_groups_from_chat_list(page: Page) -> List[Dict[str, Any]]:
    """Varre a lista lateral com scroll/filtros para capturar chats ausentes no Store."""
    collected: Dict[str, Dict[str, Any]] = {}

    try:
        pane = page.locator("#pane-side")
        if await pane.count() > 0:
            await pane.first.click(timeout=3000)
    except Exception:
        pass

    try:
        titles = await page.evaluate(SUPPLEMENT_GROUP_SCROLL_JS)
    except Exception:
        titles = []

    for title in titles or []:
        name = str(title).strip()
        if not is_plausible_group_name(name):
            continue
        key = _normalize_group_name(name)
        if not key or key in collected:
            continue
        collected[key] = {
            "whatsapp_id": None,
            "name": name,
            "type": "group",
            "source": "dom:scroll-supplement",
            "unread_count": 0,
            "archived": False,
            "pinned": False,
            "muted": False,
            "last_message_at": None,
        }

    return list(collected.values())


def _group_name_matches_wanted(found: str, wanted: str) -> bool:
    found_key = _normalize_group_name(found)
    wanted_key = _normalize_group_name(wanted)
    if not found_key or not wanted_key:
        return False
    return found_key == wanted_key or wanted_key in found_key or found_key in wanted_key


async def discover_group_by_search(page: Page, name: str) -> Optional[Dict[str, Any]]:
    """Tenta localizar um grupo pelo nome usando a busca do WhatsApp Web."""
    cleaned = (name or "").strip()
    if not cleaned:
        return None

    for query in _name_search_queries(cleaned):
        await clear_search(page)
        if not await click_search_box(page):
            continue

        await page.keyboard.press("Control+A")
        await page.keyboard.type(query, delay=20)
        await page.wait_for_timeout(2200)

        result = await page.evaluate(
            FIND_CHAT_RESULT_JS,
            {"query": query, "fullName": cleaned},
        )
        if result and int(result.get("score") or 0) >= 90:
            title = str(result.get("title") or result.get("text") or cleaned).strip()
            if _group_name_matches_wanted(title, cleaned):
                return {
                    "whatsapp_id": None,
                    "name": title or cleaned,
                    "type": "group",
                    "source": "search:discover",
                    "unread_count": 0,
                    "archived": False,
                    "pinned": False,
                    "muted": False,
                    "last_message_at": None,
                }

        titles = await page.evaluate(COLLECT_SEARCH_RESULT_TITLES_JS, query)
        for title in titles or []:
            label = str(title).strip()
            if _group_name_matches_wanted(label, cleaned):
                return {
                    "whatsapp_id": None,
                    "name": label,
                    "type": "group",
                    "source": "search:discover",
                    "unread_count": 0,
                    "archived": False,
                    "pinned": False,
                    "muted": False,
                    "last_message_at": None,
                }

    await clear_search(page)
    return None


async def supplement_groups_by_search_prefixes(
    page: Page,
    prefixes: Optional[List[str]] = None,
    *,
    existing_names: Optional[set[str]] = None,
) -> List[Dict[str, Any]]:
    """Busca por prefixos curtos para descobrir grupos ausentes no inventário."""
    existing = existing_names or set()
    collected: Dict[str, Dict[str, Any]] = {}
    tokens = prefixes or default_group_search_prefixes()

    for token in tokens:
        query = str(token or "").strip()
        if not query:
            continue

        await clear_search(page)
        if not await click_search_box(page):
            continue

        await page.keyboard.press("Control+A")
        await page.keyboard.type(query, delay=15)
        await page.wait_for_timeout(1800)

        try:
            titles = await page.evaluate(COLLECT_SEARCH_RESULT_TITLES_JS, query)
        except Exception:
            titles = []

        for title in titles or []:
            name = str(title).strip()
            key = _normalize_group_name(name)
            if len(key) < 4 or key in existing or key in collected:
                continue
            collected[key] = {
                "whatsapp_id": None,
                "name": name,
                "type": "group",
                "source": f"search:prefix:{query}",
                "unread_count": 0,
                "archived": False,
                "pinned": False,
                "muted": False,
                "last_message_at": None,
            }

    await clear_search(page)
    return list(collected.values())


def list_group_names_from_targets_file(targets_path: Path) -> List[str]:
    """Lê nomes de grupos cadastrados em um arquivo targets JSON."""
    if not targets_path.exists():
        return []

    try:
        data = json.loads(targets_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []

    if not isinstance(data, dict):
        return []

    names: List[str] = []
    seen: set[str] = set()
    for raw in data.get("targets") or []:
        if str(raw.get("type", "")).strip().lower() != "group":
            continue
        name = str(raw.get("name") or "").strip()
        key = _normalize_group_name(name)
        if not key or key in seen:
            continue
        if name.upper().startswith("NOME EXATO"):
            continue
        seen.add(key)
        names.append(name)
    return names


def group_target_id(name: str, whatsapp_id: Optional[str], used: set[str]) -> str:
    base = safe_id(name or whatsapp_id or "grupo")
    if base in used:
        suffix = 2
        candidate = f"{base}_{suffix}"
        while candidate in used:
            suffix += 1
            candidate = f"{base}_{suffix}"
        base = candidate
    used.add(base)
    return base


def phone_target_id(name: str, phone: str, used: set[str]) -> str:
    base = safe_id(f"numero_{phone}" if phone else name or "numero")
    if base in used:
        suffix = 2
        candidate = f"{base}_{suffix}"
        while candidate in used:
            suffix += 1
            candidate = f"{base}_{suffix}"
        base = candidate
    used.add(base)
    return base


def normalize_phone_digits(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    digits = re.sub(r"\D+", "", value)
    if len(digits) < 8:
        return None
    return digits


def contact_phone_digits(contact: Dict[str, Any]) -> Optional[str]:
    phone = normalize_phone_digits(str(contact.get("phone") or ""))
    if phone:
        return phone
    whatsapp_id = str(contact.get("whatsapp_id") or "")
    user = whatsapp_id.split("@")[0] if whatsapp_id else ""
    return normalize_phone_digits(user)


def default_targets_document() -> Dict[str, Any]:
    return {
        "interval_seconds": 60,
        "scrolls_per_target": 8,
        "delay_between_scrolls": 1.0,
        "delay_between_targets": 2.0,
        "append_only_new_messages": True,
        "targets": [],
    }


def merge_contacts_into_targets(
    targets_path: Path,
    contacts: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """Mescla contatos/números do WhatsApp em targets.json preservando grupos existentes."""
    if targets_path.exists():
        data = json.loads(targets_path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("Formato de targets.json inválido.")
    else:
        data = default_targets_document()

    raw_targets = data.get("targets") or []
    if not isinstance(raw_targets, list):
        raise ValueError("Campo targets inválido em targets.json.")

    non_phone_targets = [item for item in raw_targets if str(item.get("type", "")).lower() != "phone"]
    phones_by_digits: Dict[str, Dict[str, Any]] = {}
    used_ids: set[str] = set()

    for item in raw_targets:
        if str(item.get("type", "")).lower() != "phone":
            continue
        phone = normalize_phone_digits(str(item.get("phone") or ""))
        if not phone:
            continue
        phones_by_digits[phone] = item
        used_ids.add(safe_id(str(item.get("id") or phone)))

    added = 0
    updated = 0
    for contact in contacts:
        phone = contact_phone_digits(contact)
        if not phone:
            continue
        name = str(contact.get("name") or phone).strip()
        metadata = {
            "whatsapp_id": contact.get("whatsapp_id"),
            "source": contact.get("source"),
            "unread_count": contact.get("unread_count", 0),
            "archived": contact.get("archived", False),
            "pinned": contact.get("pinned", False),
            "muted": contact.get("muted", False),
            "last_message_at": contact.get("last_message_at"),
        }

        if phone in phones_by_digits:
            existing = phones_by_digits[phone]
            if name and name != phone:
                existing["name"] = name
            existing.setdefault("metadata", {}).update(metadata)
            updated += 1
            continue

        target_id = phone_target_id(name, phone, used_ids)
        phones_by_digits[phone] = {
            "id": target_id,
            "type": "phone",
            "phone": phone,
            "name": name,
            "enabled": False,
            "send": {"enabled": False, "message": ""},
            "metadata": metadata,
        }
        added += 1

    merged_phones = sorted(
        phones_by_digits.values(),
        key=lambda item: str(item.get("name") or item.get("phone") or "").casefold(),
    )
    data["targets"] = non_phone_targets + merged_phones
    targets_path.parent.mkdir(parents=True, exist_ok=True)
    targets_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    return {
        "ok": True,
        "added": added,
        "updated": updated,
        "total_phones": len(merged_phones),
        "total_targets": len(data["targets"]),
        "targets_path": str(targets_path),
    }


def merge_groups_into_targets(
    targets_path: Path,
    groups: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """Mescla grupos do inventário em targets.json preservando telefones."""
    if targets_path.exists():
        data = json.loads(targets_path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("Formato de targets.json inválido.")
    else:
        data = default_targets_document()

    raw_targets = data.get("targets") or []
    if not isinstance(raw_targets, list):
        raise ValueError("Campo targets inválido em targets.json.")

    non_group_targets = [item for item in raw_targets if str(item.get("type", "")).lower() != "group"]
    groups_by_key: Dict[str, Dict[str, Any]] = {}
    used_ids: set[str] = set()

    for item in raw_targets:
        if str(item.get("type", "")).lower() != "group":
            continue
        name_key = _normalize_group_name(str(item.get("name") or ""))
        wid = str(item.get("metadata", {}).get("whatsapp_id") or item.get("whatsapp_id") or "").lower()
        key = wid or f"name:{name_key}"
        if not key:
            continue
        groups_by_key[key] = item
        used_ids.add(safe_id(str(item.get("id") or item.get("name") or "grupo")))

    added = 0
    updated = 0
    for group in groups:
        name = str(group.get("name") or "").strip()
        name_key = _normalize_group_name(name)
        whatsapp_id = group.get("whatsapp_id")
        key = str(whatsapp_id or "").strip().lower() or f"name:{name_key}"
        if not name_key and not whatsapp_id:
            continue
        metadata = {
            "whatsapp_id": whatsapp_id,
            "source": group.get("source"),
            "unread_count": group.get("unread_count", 0),
            "archived": group.get("archived", False),
            "pinned": group.get("pinned", False),
            "muted": group.get("muted", False),
            "last_message_at": group.get("last_message_at"),
        }
        if key in groups_by_key:
            existing = groups_by_key[key]
            if name:
                existing["name"] = name
            existing.setdefault("metadata", {}).update(metadata)
            updated += 1
            continue

        target_id = group_target_id(name or str(whatsapp_id or "grupo"), str(whatsapp_id) if whatsapp_id else None, used_ids)
        groups_by_key[key] = {
            "id": target_id,
            "type": "group",
            "name": name or target_id,
            "enabled": False,
            "send": {"enabled": False, "message": ""},
            "metadata": metadata,
        }
        added += 1

    merged_groups = sorted(
        groups_by_key.values(),
        key=lambda item: str(item.get("name") or item.get("id") or "").casefold(),
    )
    data["targets"] = non_group_targets + merged_groups
    targets_path.parent.mkdir(parents=True, exist_ok=True)
    targets_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    return {
        "ok": True,
        "added": added,
        "updated": updated,
        "total_groups": len(merged_groups),
        "total_targets": len(data["targets"]),
        "targets_path": str(targets_path),
    }


def build_groups_targets_json(groups: List[Dict[str, Any]]) -> Dict[str, Any]:
    used: set[str] = set()
    targets: List[Dict[str, Any]] = []

    for group in groups:
        name = str(group.get("name") or group.get("whatsapp_id") or "").strip()
        whatsapp_id = group.get("whatsapp_id")
        if not name:
            continue
        targets.append({
            "id": group_target_id(name, str(whatsapp_id) if whatsapp_id else None, used),
            "type": "group",
            "name": name,
            "enabled": False,
            "send": {
                "enabled": False,
                "message": ""
            },
            "metadata": {
                "whatsapp_id": whatsapp_id,
                "source": group.get("source"),
                "unread_count": group.get("unread_count", 0),
                "archived": group.get("archived", False),
                "pinned": group.get("pinned", False),
                "muted": group.get("muted", False),
                "last_message_at": group.get("last_message_at"),
            }
        })

    return {
        "interval_seconds": 60,
        "scrolls_per_target": 8,
        "delay_between_scrolls": 1.0,
        "delay_between_targets": 2.0,
        "append_only_new_messages": True,
        "targets": targets,
    }


async def extract_whatsapp_groups(
    page: Page,
    *,
    resolve_names: Optional[List[str]] = None,
) -> Dict[str, Any]:
    await wait_for_whatsapp_sync_idle(page, timeout_ms=120_000)
    result = await page.evaluate(EXTRACT_WHATSAPP_CHATS_JS, "groups")
    if not isinstance(result, dict):
        return {
            "ok": False,
            "generated_at": now_iso(),
            "total_groups": 0,
            "groups": [],
            "diagnostics": {"error": "Resultado inválido retornado pelo navegador."},
        }

    groups = list(result.get("groups") or [])
    try:
        store_groups = await page.evaluate(EXTRACT_INDEXEDDB_GROUPS_JS)
    except Exception:
        store_groups = []
    if isinstance(store_groups, list) and store_groups:
        groups, store_added = merge_group_entries(groups, store_groups)
    else:
        store_added = 0

    supplement = await supplement_groups_from_chat_list(page)
    groups, scroll_added = merge_group_entries(groups, supplement)

    search_discovered: List[Dict[str, Any]] = []
    for name in resolve_names or []:
        key = _normalize_group_name(name)
        if not key:
            continue
        if any(_normalize_group_name(str(g.get("name") or "")) == key for g in groups):
            continue
        entry = await discover_group_by_search(page, name)
        if entry and is_plausible_group_name(entry.get("name")):
            search_discovered.append(entry)

    groups, search_added = merge_group_entries(groups, search_discovered)
    before_finalize = len(groups)
    groups = finalize_group_inventory(groups)

    raw_diag = result.get("diagnostics")
    diagnostics: dict[str, Any] = dict(raw_diag) if isinstance(raw_diag, dict) else {}
    diagnostics["store_deep_scan_added"] = store_added
    diagnostics["store_deep_scan_found"] = len(store_groups) if isinstance(store_groups, list) else 0
    diagnostics["scroll_supplement_added"] = scroll_added
    diagnostics["scroll_supplement_found"] = len(supplement)
    diagnostics["search_discover_added"] = search_added
    diagnostics["search_discover_found"] = len(search_discovered)
    diagnostics["finalize_dropped"] = max(0, before_finalize - len(groups))
    result["groups"] = groups
    result["diagnostics"] = diagnostics
    result["generated_at"] = now_iso()
    result["total_groups"] = len(groups)
    result["ok"] = bool(result.get("ok")) or len(groups) > 0
    return result


async def extract_whatsapp_contacts(page: Page) -> Dict[str, Any]:
    result = await page.evaluate(EXTRACT_WHATSAPP_CHATS_JS, "contacts")
    if not isinstance(result, dict):
        return {
            "ok": False,
            "generated_at": now_iso(),
            "total_contacts": 0,
            "contacts": [],
            "diagnostics": {"error": "Resultado inválido retornado pelo navegador."},
        }

    result["generated_at"] = now_iso()
    result.setdefault("contacts", [])
    result["total_contacts"] = len(result.get("contacts") or [])
    return result


async def cmd_list_groups(args: argparse.Namespace) -> None:
    app_config = load_app_config()
    app_config.export_dir.mkdir(parents=True, exist_ok=True)

    output_path = Path(args.output)
    if not output_path.is_absolute():
        output_path = PROJECT_ROOT / output_path

    targets_output_path = Path(args.targets_output) if args.targets_output else None
    if targets_output_path and not targets_output_path.is_absolute():
        targets_output_path = PROJECT_ROOT / targets_output_path

    playwright, context, page = await open_whatsapp(app_config)
    try:
        await wait_for_whatsapp_ready(page, app_config.ready_timeout)
        print("")
        print("Buscando grupos disponíveis na sessão atual do WhatsApp Web...")
        result = await extract_whatsapp_groups(page)

        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")

        groups = result.get("groups") or []
        print(f"Grupos encontrados: {len(groups)}")
        print(f"Inventário JSON: {output_path}")

        if args.print_names:
            for index, group in enumerate(groups, start=1):
                print(f"  {index:03d}. {group.get('name') or group.get('whatsapp_id')}")

        if targets_output_path:
            targets_json = build_groups_targets_json(groups)
            targets_output_path.parent.mkdir(parents=True, exist_ok=True)
            targets_output_path.write_text(json.dumps(targets_json, ensure_ascii=False, indent=2), encoding="utf-8")
            print(f"Template de targets: {targets_output_path}")

        if not groups:
            print("")
            print("Nenhum grupo foi extraído automaticamente.")
            print("Isso pode acontecer se o WhatsApp Web ainda estiver sincronizando ou se a estrutura interna da página mudou.")
            print("Deixe o WhatsApp Web terminar a sincronização e execute novamente.")
            print("O arquivo JSON foi salvo com diagnósticos técnicos para análise.")
    finally:
        await context.close()
        await playwright.stop()


async def cmd_list_contacts(args: argparse.Namespace) -> None:
    app_config = load_app_config()
    app_config.export_dir.mkdir(parents=True, exist_ok=True)

    output_path = Path(args.output)
    if not output_path.is_absolute():
        output_path = PROJECT_ROOT / output_path

    targets_path = Path(args.targets)
    if not targets_path.is_absolute():
        targets_path = PROJECT_ROOT / targets_path

    playwright, context, page = await open_whatsapp(app_config)
    try:
        await wait_for_whatsapp_ready(page, app_config.ready_timeout)
        print("")
        print("Buscando números/contatos disponíveis na sessão atual do WhatsApp Web...")
        result = await extract_whatsapp_contacts(page)

        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")

        contacts = result.get("contacts") or []
        print(f"Contatos encontrados: {len(contacts)}")
        print(f"Inventário JSON: {output_path}")

        if args.print_names:
            for index, contact in enumerate(contacts, start=1):
                label = contact.get("name") or contact.get("phone") or contact.get("whatsapp_id")
                print(f"  {index:03d}. {label}")

        merge_outcome = merge_contacts_into_targets(targets_path, contacts)
        print(f"Targets atualizado: {targets_path}")
        print(
            f"Números no targets.json: {merge_outcome['total_phones']} "
            f"(+{merge_outcome['added']} novos, {merge_outcome['updated']} atualizados)"
        )

        if not contacts:
            print("")
            print("Nenhum contato foi extraído automaticamente.")
            print("Deixe o WhatsApp Web terminar a sincronização e execute novamente.")
    finally:
        await context.close()
        await playwright.stop()


async def process_send_target(
    page: Page | None,
    app_config: AppConfig,
    target: Target,
    message: str,
    *,
    dry_run: bool,
    attachment_path: str | None = None,
) -> Dict[str, Any]:
    print("")
    print(f"=== Envio: {target.id} | tipo={target.type} | destino={target_display_name(target)} ===")

    has_message = bool((message or "").strip())
    has_attachment = bool(attachment_path)
    if not has_message and not has_attachment:
        record = build_send_record(
            target,
            message,
            {"ok": False, "error": "Informe uma mensagem ou anexo."},
            dry_run=dry_run,
            attachment=attachment_path,
        )
        return record

    if dry_run:
        print("Simulação ativada: a conversa não será aberta e nada será enviado.")
        if has_message:
            print(f"Mensagem: {message}")
        if has_attachment:
            print(f"Anexo: {attachment_path}")
        return build_send_record(
            target,
            message,
            {"ok": True},
            dry_run=True,
            attachment=attachment_path,
        )

    if page is None:
        return build_send_record(
            target,
            message,
            {"ok": False, "error": "Página Playwright indisponível para envio real."},
            dry_run=False,
            attachment=attachment_path,
        )

    open_error: Optional[str] = None
    open_message = None if has_attachment else (message if has_message else None)
    if target.type == "phone":
        opened, open_error = await open_chat_by_phone(page, target.phone or "", message=open_message)
    else:
        opened, open_error = await open_target(page, target)
    if not opened:
        record = build_send_record(
            target,
            message,
            {"ok": False, "error": open_error or "Não foi possível abrir a conversa."},
            dry_run=False,
            attachment=attachment_path,
        )
        try:
            fail_dir = app_config.export_dir / "send" / "debug"
            fail_dir.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            screenshot_path = fail_dir / f"falha_abertura_{target.id}_{stamp}.png"
            await page.screenshot(path=str(screenshot_path), full_page=True)
            record["screenshot"] = str(screenshot_path)
            record["page_url"] = page.url
            print(f"Screenshot de diagnóstico: {screenshot_path}")
        except Exception:
            pass
        return record

    await page.wait_for_timeout(1800)
    if has_attachment:
        resolved = resolve_project_path(Path(attachment_path or ""))
        result = await send_attachment_to_current_chat(
            page,
            resolved,
            caption=message if has_message else "",
        )
    else:
        result = await send_text_to_current_chat(page, message)
    record = build_send_record(target, message, result, dry_run=False, attachment=attachment_path)

    if record["ok"]:
        print(f"Mensagem enviada, apareceu no chat e não está pendente para {target_display_name(target)}.")
    else:
        try:
            fail_dir = app_config.export_dir / "send" / "debug"
            fail_dir.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            screenshot_path = fail_dir / f"falha_envio_{target.id}_{stamp}.png"
            await page.screenshot(path=str(screenshot_path), full_page=True)
            record["screenshot"] = str(screenshot_path)
            record["page_url"] = page.url
            print(f"Falha ao enviar para {target_display_name(target)}: {record.get('error')}")
            print(f"Screenshot de diagnóstico: {screenshot_path}")
        except Exception:
            print(f"Falha ao enviar para {target_display_name(target)}: {record.get('error')}")

    return record


async def run_send_once(
    page: Page | None,
    app_config: AppConfig,
    targets_config: TargetsConfig,
    *,
    message_override: Optional[str] = None,
    target_ids: Optional[List[str]] = None,
    dry_run: bool = True,
    attachment_path: Optional[str] = None,
) -> List[Dict[str, Any]]:
    selected_ids = {safe_id(item) for item in (target_ids or []) if item}
    if selected_ids:
        selected_targets = [t for t in targets_config.targets if t.id in selected_ids]
    else:
        selected_targets = [t for t in targets_config.targets if t.enabled]

    send_targets: List[Tuple[Target, str]] = []
    for target in selected_targets:
        if message_override is not None:
            send_targets.append((target, message_override))
        elif attachment_path:
            send_targets.append((target, target.message or ""))
        elif target.send_enabled and target.message:
            send_targets.append((target, target.message))

    print("")
    print(f"Envio iniciado às {now_iso()}. Alvos selecionados: {len(send_targets)}")
    if attachment_path:
        print(f"Anexo: {attachment_path}")

    if not send_targets:
        print("Nenhum alvo configurado para envio.")
        print("Use --message, anexo ou configure send.enabled=true e send.message no targets.json.")
        return []

    results: List[Dict[str, Any]] = []
    for target, message in send_targets:
        try:
            record = await process_send_target(
                page=page,
                app_config=app_config,
                target=target,
                message=message,
                dry_run=dry_run,
                attachment_path=attachment_path,
            )
            results.append(record)
            append_jsonl(app_config.export_dir / "send" / "sent_log.jsonl", [record])
        except Exception as exc:
            print(f"Falha no envio para {target.id}: {type(exc).__name__}: {exc}")
            record = build_send_record(
                target,
                message,
                {"ok": False, "error": f"{type(exc).__name__}: {exc}"},
                dry_run=dry_run,
                attachment=attachment_path,
            )
            results.append(record)
            append_jsonl(app_config.export_dir / "send" / "sent_log.jsonl", [record])

        if page is not None:
            await page.wait_for_timeout(int(targets_config.delay_between_targets * 1000))

    send_dir = app_config.export_dir / "send"
    send_dir.mkdir(parents=True, exist_ok=True)
    write_send_latest(send_dir / "last_send.json", results)

    total_ok = sum(1 for item in results if item.get("ok"))
    print("")
    print(f"Envio finalizado. Sucessos: {total_ok}/{len(results)}")
    print(f"Resumo: {send_dir / 'last_send.json'}")

    return results


async def process_target(
    page: Page,
    app_config: AppConfig,
    targets_config: TargetsConfig,
    state: MessageState,
    target: Target,
) -> Dict[str, Any]:
    print("")
    print(f"=== Alvo: {target.id} | tipo={target.type} | nome={target.name or target.phone} ===")

    opened, open_error = await open_target(page, target)
    if not opened:
        return {
            "target_id": target.id,
            "ok": False,
            "error": open_error or "Não foi possível abrir a conversa.",
            "new_messages": 0,
            "captured_messages": 0,
        }

    await page.wait_for_timeout(1800)

    messages = await collect_messages_for_target(
        page=page,
        target=target,
        scrolls=targets_config.scrolls_per_target,
        delay=targets_config.delay_between_scrolls,
    )

    seen = state.seen_hashes(target.id)

    if targets_config.append_only_new_messages:
        new_messages = [m for m in messages if m["hash"] not in seen]
    else:
        new_messages = messages

    messages_dir = app_config.export_dir / "messages"
    jsonl_path = messages_dir / f"{target.id}.jsonl"
    latest_path = messages_dir / f"{target.id}_latest.json"
    csv_path = messages_dir / f"{target.id}_latest.csv"

    if new_messages:
        append_jsonl(jsonl_path, new_messages)
        state.add_hashes(target.id, [m["hash"] for m in new_messages])
        state.save()

    write_latest_json(latest_path, messages)
    write_csv(csv_path, messages)

    print(f"Capturadas no ciclo: {len(messages)}")
    print(f"Novas gravadas:      {len(new_messages)}")
    print(f"JSONL incremental:   {jsonl_path}")
    print(f"JSON latest:         {latest_path}")
    print(f"CSV latest:          {csv_path}")

    return {
        "target_id": target.id,
        "ok": True,
        "new_messages": len(new_messages),
        "captured_messages": len(messages),
        "jsonl": str(jsonl_path),
        "latest_json": str(latest_path),
        "latest_csv": str(csv_path),
    }


async def run_cycle(
    page: Page,
    app_config: AppConfig,
    targets_config: TargetsConfig,
    state: MessageState,
) -> List[Dict[str, Any]]:
    enabled_targets = [t for t in targets_config.targets if t.enabled]

    print("")
    print(f"Iniciando ciclo às {now_iso()}. Alvos habilitados: {len(enabled_targets)}")

    results: List[Dict[str, Any]] = []

    for target in enabled_targets:
        try:
            result = await process_target(page, app_config, targets_config, state, target)
            results.append(result)
        except Exception as exc:
            print(f"Falha no alvo {target.id}: {type(exc).__name__}: {exc}")
            results.append({
                "target_id": target.id,
                "ok": False,
                "error": f"{type(exc).__name__}: {exc}",
            })

        await page.wait_for_timeout(int(targets_config.delay_between_targets * 1000))

    summary_dir = app_config.export_dir / "summary"
    summary_dir.mkdir(parents=True, exist_ok=True)
    summary_path = summary_dir / "last_cycle.json"
    summary_path.write_text(json.dumps({
        "finished_at": now_iso(),
        "results": results,
    }, ensure_ascii=False, indent=2), encoding="utf-8")

    total_new = sum(int(r.get("new_messages", 0)) for r in results)
    print("")
    print(f"Ciclo finalizado. Total de mensagens novas: {total_new}")
    print(f"Resumo: {summary_path}")

    return results


async def cmd_doctor(args: argparse.Namespace) -> None:
    app_config = load_app_config()
    print("=== Diagnóstico ===")
    print(f"Python: {sys.version.split()[0]}")
    print(f"Sistema: {platform.platform()}")
    print(f"Projeto: {PROJECT_ROOT}")
    print(f"Perfil: {app_config.profile_dir}")
    print(f"Export: {app_config.export_dir}")
    print(f"State:  {app_config.state_dir}")
    print(f"Headless: {app_config.headless}")
    print("")

    locks = existing_lock_files(app_config.profile_dir)
    if locks:
        print("Arquivos de lock encontrados:")
        for lock in locks:
            print(f"  - {lock}")
    else:
        print("Nenhum lock comum encontrado.")

    processes = find_profile_processes(app_config.profile_dir)
    print("")
    if processes:
        print("Processos usando o perfil:")
        for proc in processes:
            print(f"  - PID {proc.get('ProcessId')} | {proc.get('Name')}")
    else:
        print("Nenhum processo usando diretamente esse perfil foi encontrado.")

    if platform.system().lower().startswith("win"):
        pw = find_playwright_chromium_processes_windows()
        print("")
        if pw:
            print("Chromiums residuais do Playwright:")
            for proc in pw:
                print(f"  - PID {proc.get('ProcessId')} | {proc.get('Name')}")
        else:
            print("Nenhum Chromium residual do Playwright encontrado.")


async def cmd_unlock_profile(args: argparse.Namespace) -> None:
    app_config = load_app_config()
    print(f"Perfil: {app_config.profile_dir}")

    total_killed = 0

    processes = find_profile_processes(app_config.profile_dir)
    if processes:
        print("Processos usando o perfil:")
        for proc in processes:
            print(f"  - PID {proc.get('ProcessId')} | {proc.get('Name')}")
        if args.kill:
            total_killed += kill_processes(processes)
    else:
        print("Nenhum processo usando diretamente o perfil.")

    if args.kill_playwright and platform.system().lower().startswith("win"):
        pw = find_playwright_chromium_processes_windows()
        if pw:
            print("Chromiums residuais do Playwright:")
            for proc in pw:
                print(f"  - PID {proc.get('ProcessId')} | {proc.get('Name')}")
            total_killed += kill_processes_windows(pw)
        else:
            print("Nenhum Chromium residual do Playwright encontrado.")

    if args.remove_locks:
        removed = remove_lock_files(app_config.profile_dir)
        if removed:
            print("Locks removidos:")
            for item in removed:
                print(f"  - {item}")
        else:
            print("Nenhum lock removido.")

    print(f"Total de processos encerrados: {total_killed}")


async def cmd_run_once(args: argparse.Namespace) -> None:
    app_config = load_app_config()
    targets_config = load_targets_config(Path(args.targets))
    app_config.export_dir.mkdir(parents=True, exist_ok=True)
    app_config.state_dir.mkdir(parents=True, exist_ok=True)

    state = MessageState(app_config.state_dir / "message_state.json")

    playwright, context, page = await open_whatsapp(app_config)
    try:
        await wait_for_whatsapp_ready(page, app_config.ready_timeout)
        await run_cycle(page, app_config, targets_config, state)
    finally:
        await context.close()
        await playwright.stop()


async def cmd_scan(args: argparse.Namespace) -> None:
    app_config = load_app_config()
    targets_config = load_targets_config(Path(args.targets))
    app_config.export_dir.mkdir(parents=True, exist_ok=True)
    app_config.state_dir.mkdir(parents=True, exist_ok=True)

    state = MessageState(app_config.state_dir / "message_state.json")

    playwright, context, page = await open_whatsapp(app_config)
    try:
        await wait_for_whatsapp_ready(page, app_config.ready_timeout)

        print("")
        print("Varredura contínua iniciada.")
        print(f"Arquivo de alvos: {args.targets}")
        print(f"Intervalo entre ciclos: {targets_config.interval_seconds}s")
        print("Pressione CTRL+C para encerrar.")

        while True:
            start = time.time()
            await run_cycle(page, app_config, targets_config, state)

            elapsed = time.time() - start
            sleep_for = max(1, targets_config.interval_seconds - int(elapsed))
            print(f"Aguardando {sleep_for}s para próximo ciclo...")
            await asyncio.sleep(sleep_for)

    except KeyboardInterrupt:
        print("Varredura encerrada pelo usuário.")
    finally:
        await context.close()
        await playwright.stop()


async def cmd_send_once(args: argparse.Namespace) -> None:
    app_config = load_app_config()
    targets_config = load_targets_config(Path(args.targets))
    app_config.export_dir.mkdir(parents=True, exist_ok=True)

    if args.message and not args.target_id and not args.all:
        print("Por segurança, --message sem --target-id não envia para todos automaticamente.")
        print("Informe --target-id uma ou mais vezes, ou use --all de forma explícita.")
        return

    dry_run = not bool(args.confirm)
    if dry_run:
        print("ATENÇÃO: modo simulação. Nenhuma mensagem será enviada.")
        print("Para enviar de fato, execute novamente com --confirm.")

    if dry_run:
        await run_send_once(
            page=None,
            app_config=app_config,
            targets_config=targets_config,
            message_override=args.message,
            target_ids=args.target_id,
            dry_run=True,
        )
        return

    playwright, context, page = await open_whatsapp(app_config)
    try:
        await wait_for_whatsapp_ready(page, app_config.ready_timeout)
        await run_send_once(
            page=page,
            app_config=app_config,
            targets_config=targets_config,
            message_override=args.message,
            target_ids=args.target_id,
            dry_run=False,
        )
    finally:
        await context.close()
        await playwright.stop()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="WhatsApp Web Automation v8 — varredura automática, envio e inventário de grupos."
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_doctor = sub.add_parser("doctor", help="Diagnostica perfil, locks e processos.")
    p_doctor.set_defaults(func=cmd_doctor)

    p_unlock = sub.add_parser("unlock-profile", help="Desbloqueia perfil e remove locks.")
    p_unlock.add_argument("--kill", action="store_true", help="Encerra processos usando o perfil.")
    p_unlock.add_argument("--kill-playwright", action="store_true", help="Encerra Chromiums residuais do Playwright.")
    p_unlock.add_argument("--remove-locks", action="store_true", help="Remove arquivos de lock órfãos.")
    p_unlock.set_defaults(func=cmd_unlock_profile)

    p_once = sub.add_parser("run-once", help="Executa uma varredura única nos alvos configurados.")
    p_once.add_argument("--targets", default="config/targets.json", help="Arquivo JSON de alvos.")
    p_once.set_defaults(func=cmd_run_once)

    p_scan = sub.add_parser("scan", help="Executa varredura contínua nos alvos configurados.")
    p_scan.add_argument("--targets", default="config/targets.json", help="Arquivo JSON de alvos.")
    p_scan.set_defaults(func=cmd_scan)

    p_send = sub.add_parser("send-once", help="Envia uma mensagem uma vez para alvos configurados.")
    p_send.add_argument("--targets", default="config/targets.json", help="Arquivo JSON de alvos.")
    p_send.add_argument("--message", default=None, help="Mensagem global para enviar aos alvos selecionados.")
    p_send.add_argument(
        "--target-id",
        action="append",
        default=None,
        help="ID do alvo a receber a mensagem. Pode ser usado mais de uma vez.",
    )
    p_send.add_argument(
        "--all",
        action="store_true",
        help="Permite usar --message para todos os alvos habilitados de forma explícita.",
    )
    p_send.add_argument(
        "--confirm",
        action="store_true",
        help="Confirma o envio real. Sem esta opção, roda em modo simulação.",
    )
    p_send.set_defaults(func=cmd_send_once)

    p_groups = sub.add_parser("list-groups", help="Lista grupos disponíveis no WhatsApp Web e gera JSON.")
    p_groups.add_argument(
        "--output",
        default="exports/groups/groups.json",
        help="Arquivo JSON de inventário dos grupos encontrados.",
    )
    p_groups.add_argument(
        "--targets-output",
        default="exports/groups/groups_targets_template.json",
        help="Arquivo JSON opcional no formato targets.json com os grupos encontrados.",
    )
    p_groups.add_argument(
        "--no-targets-output",
        dest="targets_output",
        action="store_const",
        const=None,
        help="Não gera o arquivo de template de targets.",
    )
    p_groups.add_argument(
        "--print-names",
        action="store_true",
        help="Mostra no terminal o nome dos grupos encontrados.",
    )
    p_groups.set_defaults(func=cmd_list_groups)

    p_contacts = sub.add_parser(
        "list-contacts",
        help="Lista números/contatos do WhatsApp Web e atualiza config/targets.json.",
    )
    p_contacts.add_argument(
        "--output",
        default="exports/contacts/contacts.json",
        help="Arquivo JSON de inventário dos contatos encontrados.",
    )
    p_contacts.add_argument(
        "--targets",
        default="config/targets.json",
        help="Arquivo targets.json que será atualizado com os números encontrados.",
    )
    p_contacts.add_argument(
        "--print-names",
        action="store_true",
        help="Mostra no terminal o nome/telefone dos contatos encontrados.",
    )
    p_contacts.set_defaults(func=cmd_list_contacts)

    return parser


async def main_async() -> None:
    parser = build_parser()
    args = parser.parse_args()
    await args.func(args)


def main() -> None:
    asyncio.run(main_async())


if __name__ == "__main__":
    main()
