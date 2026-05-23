"""
WhatsApp Web Automation v4 — varredura automática de grupos/contatos/telefones.

Objetivo:
- Abrir automaticamente conversas configuradas em config/targets.json.
- Capturar mensagens visíveis e histórico via rolagem.
- Salvar apenas mensagens novas por alvo.
- Repetir continuamente em ciclos.

Uso:
    python src/whatsapp_auto_downloader.py doctor
    python src/whatsapp_auto_downloader.py unlock-profile --kill --kill-playwright --remove-locks
    python src/whatsapp_auto_downloader.py run-once --targets config/targets.json
    python src/whatsapp_auto_downloader.py scan --targets config/targets.json

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

from dotenv import load_dotenv
from playwright.async_api import async_playwright, Page, BrowserContext, Playwright
from playwright.async_api import TimeoutError as PlaywrightTimeoutError


WA_URL = "https://web.whatsapp.com/"
PROJECT_ROOT = Path(__file__).resolve().parents[1]


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

        targets.append(Target(
            id=target_id,
            type=target_type,
            name=raw.get("name"),
            phone=str(raw.get("phone")).strip() if raw.get("phone") else None,
            enabled=bool(raw.get("enabled", True)),
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


async def open_chat_by_phone(page: Page, phone: str) -> bool:
    clean = re.sub(r"\D+", "", phone or "")
    if not clean:
        print("Telefone vazio/inválido.")
        return False

    print(f"Abrindo conversa por telefone: {clean}")
    await page.goto(f"{WA_URL}send?phone={clean}", wait_until="domcontentloaded")
    await page.wait_for_timeout(5000)

    # Se aparecer botão de continuar/usar WhatsApp Web, tenta confirmar.
    try:
        for text in ["Continuar", "Continue", "Usar o WhatsApp Web", "use WhatsApp Web"]:
            locator = page.get_by_text(text, exact=False)
            if await locator.count() > 0:
                await locator.first.click(timeout=2000)
                await page.wait_for_timeout(3000)
                break
    except Exception:
        pass

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


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="WhatsApp Web Automation v4 — varredura automática por alvos."
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

    return parser


async def main_async() -> None:
    parser = build_parser()
    args = parser.parse_args()
    await args.func(args)


def main() -> None:
    asyncio.run(main_async())


if __name__ == "__main__":
    main()
