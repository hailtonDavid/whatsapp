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
    load_dotenv(env_file or PROJECT_ROOT / ".env")

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
        headless=str_to_bool(os.getenv("WA_HEADLESS"), default=False),
        ready_timeout=int(os.getenv("WA_READY_TIMEOUT", "180")),
        export_dir=export_dir,
        state_dir=state_dir,
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
            user_data_dir=str(config.profile_dir),
            headless=config.headless,
            viewport={"width": 1440, "height": 900},
            locale="pt-BR",
            args=[
                "--no-default-browser-check",
                "--disable-infobars",
            ],
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
(targetName) => {
    const query = String(targetName || "").trim().toLowerCase();

    const visible = (el) => {
        const r = el.getBoundingClientRect();
        const s = window.getComputedStyle(el);
        return r.width > 0 && r.height > 0 &&
               s.display !== "none" &&
               s.visibility !== "hidden" &&
               r.x < window.innerWidth * 0.60 &&
               r.y > 60;
    };

    const normalize = (txt) => String(txt || "")
        .replace(/\\s+/g, " ")
        .trim()
        .toLowerCase();

    const candidates = Array.from(document.querySelectorAll(
        "div[role='row'], div[role='gridcell'], div[tabindex], span[title], div"
    ))
    .filter(visible)
    .map((el) => {
        const r = el.getBoundingClientRect();
        const text = normalize(el.innerText || el.textContent || "");
        const title = normalize(el.getAttribute("title") || "");
        const aria = normalize(el.getAttribute("aria-label") || "");
        const joined = [text, title, aria].filter(Boolean).join(" | ");

        let score = 0;
        if (joined === query) score += 100;
        if (title === query) score += 90;
        if (joined.includes(query)) score += 40;
        if (text.includes(query)) score += 30;
        if (r.x < window.innerWidth * 0.45) score += 5;
        if (r.width > 100 && r.height > 30) score += 2;

        return {
            score,
            text,
            title,
            aria,
            x: r.x,
            y: r.y,
            width: r.width,
            height: r.height
        };
    })
    .filter(x => x.score >= 30)
    .sort((a, b) => b.score - a.score || a.y - b.y);

    return candidates[0] || null;
}
"""


async def open_chat_by_name(page: Page, name: str) -> bool:
    print(f"Buscando conversa: {name}")

    await clear_search(page)

    ok = await click_search_box(page)
    if not ok:
        print("Não encontrei a caixa de pesquisa.")
        return False

    await page.keyboard.press("Control+A")
    await page.keyboard.type(name, delay=25)
    await page.wait_for_timeout(1500)

    result = await page.evaluate(FIND_CHAT_RESULT_JS, name)
    if not result:
        print(f"Nenhum resultado encontrado para: {name}")
        return False

    await page.mouse.click(
        result["x"] + min(50, result["width"] / 2),
        result["y"] + result["height"] / 2,
    )
    await page.wait_for_timeout(1800)
    return True


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


async def open_chat_by_phone(page: Page, phone: str, message: Optional[str] = None) -> bool:
    clean = re.sub(r"\D+", "", phone or "")
    if not clean:
        print("Telefone vazio/inválido.")
        return False

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
        print(f"Conversa por telefone não ficou pronta: {ready.get('error')}")
        return False

    return True


async def open_target(page: Page, target: Target) -> bool:
    if target.type in {"group", "contact", "name"}:
        if not target.name:
            print(f"Alvo {target.id} sem campo name.")
            return False
        return await open_chat_by_name(page, target.name)

    if target.type == "phone":
        if not target.phone:
            print(f"Alvo {target.id} sem campo phone.")
            return False
        return await open_chat_by_phone(page, target.phone)

    print(f"Tipo de alvo não suportado: {target.type}")
    return False


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
        ok: !invalidReason && hasMessageBox,
        has_message_box: hasMessageBox,
        invalid_reason: invalidReason,
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
            "span.selectable-text.copyable-text, span.selectable-text, span[dir='ltr'], span[dir='auto']"
        ));

        const texts = nodes
            .map(n => normalize(n.innerText || n.textContent || ""))
            .filter(Boolean);

        if (texts.length) return normalize(texts.join("\\n"));

        return normalize(el.innerText || el.textContent || "");
    };

    const primary = Array.from(document.querySelectorAll(
        "div.copyable-text[data-pre-plain-text], div[data-pre-plain-text]"
    ));

    const fallback = Array.from(document.querySelectorAll(
        "div.message-in, div.message-out"
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


def build_send_record(target: Target, message: str, result: Dict[str, Any], *, dry_run: bool) -> Dict[str, Any]:
    return {
        "created_at": now_iso(),
        "target_id": target.id,
        "target_type": target.type,
        "target_name": target.name,
        "target_phone": target.phone,
        "message": message,
        "message_chars": len(message or ""),
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


EXTRACT_WHATSAPP_GROUPS_JS = r"""
async () => {
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

    const makeGroup = (chat, source) => {
        const whatsappId = readId(chat);
        const name = readName(chat);
        return {
            whatsapp_id: whatsappId || null,
            name: name || whatsappId || null,
            type: "group",
            source,
            unread_count: Number(chat?.unreadCount ?? chat?.__x_unreadCount ?? chat?.unreadMsgs ?? 0) || 0,
            archived: readBool(chat?.archive, chat?.isArchived, chat?.__x_archive),
            pinned: readBool(chat?.pin, chat?.isPinned, chat?.__x_pin),
            muted: readBool(chat?.mute, chat?.isMuted, chat?.__x_mute),
            last_message_at: readTimestamp(chat),
            raw_preview: toPlain(chat, 0)
        };
    };

    const seenCollections = new WeakSet();
    const seenObjects = new WeakSet();
    const collections = [];
    const diagnostics = {
        store_present: Boolean(window.Store),
        wpp_present: Boolean(window.WPP),
        webpack_cache_modules: 0,
        collections_found: 0,
        indexeddb_groups_found: 0,
        errors: []
    };

    const addCollection = (value, label) => {
        if (!value || typeof value !== "object") return;
        if (seenCollections.has(value)) return;
        const items = objectValuesSafe(value);
        if (!items.length) return;
        const sample = items.slice(0, 80);
        const groupCount = sample.filter(isGroupChat).length;
        const idLikeCount = sample.filter(item => readId(item)).length;
        if (groupCount > 0 || idLikeCount >= Math.min(5, sample.length)) {
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

    const groupsByIdOrName = new Map();
    for (const collection of collections) {
        for (const item of collection.items) {
            if (!isGroupChat(item)) continue;
            const group = makeGroup(item, collection.label);
            const key = group.whatsapp_id || group.name;
            if (!key) continue;
            if (!groupsByIdOrName.has(key)) groupsByIdOrName.set(key, group);
        }
    }
    diagnostics.collections_found = collections.length;

    // 2) Fallback: varre IndexedDB da sessão do WhatsApp Web procurando registros com @g.us.
    // É limitado para não travar o navegador; serve como complemento quando o Store interno muda.
    const scanIndexedDb = async () => {
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
                    const rows = await getAllLimited(store, 2500);
                    for (const row of rows) {
                        let text = "";
                        try { text = JSON.stringify(row); } catch { continue; }
                        if (!text.includes("@g.us")) continue;
                        const idMatch = text.match(/[0-9]{5,}@[a-z.]?g\.us|[0-9]{5,}@g\.us/g);
                        const ids = Array.from(new Set(idMatch || []));
                        for (const id of ids) {
                            out.push({
                                whatsapp_id: id,
                                name: normalize(row?.name || row?.formattedTitle || row?.subject || row?.pushname || row?.__x_name || row?.__x_formattedTitle || row?.__x_subject || id),
                                type: "group",
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
        const idbGroups = await scanIndexedDb();
        diagnostics.indexeddb_groups_found = idbGroups.length;
        for (const group of idbGroups) {
            const key = group.whatsapp_id || group.name;
            if (key && !groupsByIdOrName.has(key)) groupsByIdOrName.set(key, group);
        }
    } catch (e) {
        diagnostics.errors.push(`indexeddb_scan: ${e?.message || e}`);
    }

    const groups = Array.from(groupsByIdOrName.values())
        .filter(g => g.name || g.whatsapp_id)
        .sort((a, b) => String(a.name || a.whatsapp_id).localeCompare(String(b.name || b.whatsapp_id), "pt-BR"));

    return {
        ok: groups.length > 0,
        generated_at_browser: new Date().toISOString(),
        total_groups: groups.length,
        groups,
        diagnostics
    };
}
"""


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


async def extract_whatsapp_groups(page: Page) -> Dict[str, Any]:
    result = await page.evaluate(EXTRACT_WHATSAPP_GROUPS_JS)
    if not isinstance(result, dict):
        return {
            "ok": False,
            "generated_at": now_iso(),
            "total_groups": 0,
            "groups": [],
            "diagnostics": {"error": "Resultado inválido retornado pelo navegador."},
        }

    result["generated_at"] = now_iso()
    result.setdefault("groups", [])
    result["total_groups"] = len(result.get("groups") or [])
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


async def process_send_target(
    page: Page,
    app_config: AppConfig,
    target: Target,
    message: str,
    *,
    dry_run: bool,
) -> Dict[str, Any]:
    print("")
    print(f"=== Envio: {target.id} | tipo={target.type} | destino={target_display_name(target)} ===")

    if not message.strip():
        record = build_send_record(
            target,
            message,
            {"ok": False, "error": "Mensagem vazia."},
            dry_run=dry_run,
        )
        return record

    if dry_run:
        print("Simulação ativada: a conversa não será aberta e a mensagem não será enviada.")
        print(f"Mensagem: {message}")
        return build_send_record(target, message, {"ok": True}, dry_run=True)

    if target.type == "phone":
        opened = await open_chat_by_phone(page, target.phone or "", message=message)
    else:
        opened = await open_target(page, target)
    if not opened:
        record = build_send_record(
            target,
            message,
            {"ok": False, "error": "Não foi possível abrir a conversa."},
            dry_run=False,
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
    result = await send_text_to_current_chat(page, message)
    record = build_send_record(target, message, result, dry_run=False)

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
    page: Page,
    app_config: AppConfig,
    targets_config: TargetsConfig,
    *,
    message_override: Optional[str] = None,
    target_ids: Optional[List[str]] = None,
    dry_run: bool = True,
) -> List[Dict[str, Any]]:
    selected_ids = {safe_id(item) for item in (target_ids or []) if item}
    enabled_targets = [t for t in targets_config.targets if t.enabled]

    if selected_ids:
        enabled_targets = [t for t in enabled_targets if t.id in selected_ids]

    send_targets: List[Tuple[Target, str]] = []
    for target in enabled_targets:
        if message_override is not None:
            send_targets.append((target, message_override))
        elif target.send_enabled and target.message:
            send_targets.append((target, target.message))

    print("")
    print(f"Envio iniciado às {now_iso()}. Alvos selecionados: {len(send_targets)}")

    if not send_targets:
        print("Nenhum alvo configurado para envio.")
        print("Use --message ou configure send.enabled=true e send.message no targets.json.")
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

    opened = await open_target(page, target)
    if not opened:
        return {
            "target_id": target.id,
            "ok": False,
            "error": "Não foi possível abrir a conversa.",
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

    return parser


async def main_async() -> None:
    parser = build_parser()
    args = parser.parse_args()
    await args.func(args)


def main() -> None:
    asyncio.run(main_async())


if __name__ == "__main__":
    main()
