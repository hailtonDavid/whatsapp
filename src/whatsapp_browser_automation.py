"""
WhatsApp Web Browser Automation v3 — uso próprio/autorizado.

Principais correções:
- Usa profile_whatsapp_v3 por padrão para fugir do profile antigo travado.
- Inclui unlock-profile para remover locks órfãos e encerrar processos do perfil.
- Evita a necessidade de rodar login e watch em duas sessões simultâneas.
- Mantém a leitura restrita ao WhatsApp Web aberto e autenticado pelo próprio usuário.

Comandos:
    python src/whatsapp_browser_automation.py doctor
    python src/whatsapp_browser_automation.py unlock-profile --kill --remove-locks
    python src/whatsapp_browser_automation.py watch-current --interval 5
    python src/whatsapp_browser_automation.py download-current --scrolls 30
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import hashlib
import json
import os
import platform
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Tuple

from dotenv import load_dotenv
from playwright.async_api import async_playwright, Page, BrowserContext, Playwright
from playwright.async_api import TimeoutError as PlaywrightTimeoutError


WA_URL = "https://web.whatsapp.com/"
PROJECT_ROOT = Path(__file__).resolve().parents[1]
EXPORT_DIR = PROJECT_ROOT / "exports"


def str_to_bool(value: str | None, default: bool = False) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "sim", "s"}


@dataclass
class AppConfig:
    profile_dir: Path
    headless: bool
    ready_timeout: int


def load_config() -> AppConfig:
    load_dotenv(PROJECT_ROOT / ".env")

    # v3 usa perfil novo por padrão para não conflitar com profile_whatsapp antigo.
    profile_dir = Path(os.getenv("WA_PROFILE_DIR", "profile_whatsapp_v3"))
    if not profile_dir.is_absolute():
        profile_dir = PROJECT_ROOT / profile_dir

    return AppConfig(
        profile_dir=profile_dir,
        headless=str_to_bool(os.getenv("WA_HEADLESS"), default=True),
        ready_timeout=int(os.getenv("WA_READY_TIMEOUT", "180")),
    )


def lock_file_candidates(profile_dir: Path) -> List[Path]:
    names = [
        "SingletonLock",
        "SingletonCookie",
        "SingletonSocket",
        "LOCK",
        "lockfile",
    ]
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


def normalize_win_path_variants(path: Path) -> List[str]:
    full = str(path.resolve())
    return list({
        full,
        full.replace("\\", "/"),
        str(path),
        str(path).replace("\\", "/"),
    })


def find_profile_processes_windows(profile_dir: Path) -> List[Dict[str, Any]]:
    variants = normalize_win_path_variants(profile_dir)
    variants_json = json.dumps(variants, ensure_ascii=False)

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
    variants = normalize_win_path_variants(profile_dir)
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

O Chromium informou que já existe uma sessão usando esse perfil ou que restaram arquivos de lock.

Correção recomendada:
  1. Feche qualquer janela do Chromium aberta por este projeto.
  2. Execute:
       python src\\whatsapp_browser_automation.py unlock-profile --kill --remove-locks
  3. Confirme no .env:
       WA_PROFILE_DIR=profile_whatsapp_v3
  4. Execute novamente:
       python src\\whatsapp_browser_automation.py watch-current --interval 5

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
        message = str(exc)
        if (
            "Target page, context or browser has been closed" in message
            or "Abrindo em uma sessão de navegador existente" in message
            or "Opening in existing browser session" in message
            or "ProcessSingleton" in message
            or "session" in message.lower()
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
        print("Não foi possível confirmar o carregamento da interface dentro do tempo limite.")
        print("Verifique QR Code, conexão e se o WhatsApp Web abriu corretamente.")


async def cmd_doctor(args: argparse.Namespace) -> None:
    config = load_config()

    print("=== Diagnóstico ===")
    print(f"Python: {sys.version.split()[0]}")
    print(f"Sistema: {platform.platform()}")
    print(f"Projeto: {PROJECT_ROOT}")
    print(f"Perfil configurado: {config.profile_dir}")
    print(f"Headless: {config.headless}")
    print("")

    locks = existing_lock_files(config.profile_dir)
    if locks:
        print("Arquivos de lock encontrados:")
        for lock in locks:
            print(f"  - {lock}")
    else:
        print("Nenhum lock comum encontrado no perfil configurado.")

    print("")
    processes = find_profile_processes(config.profile_dir)
    if processes:
        print("Processos usando diretamente o perfil configurado:")
        for proc in processes:
            print(f"  - PID {proc.get('ProcessId')} | {proc.get('Name')}")
    else:
        print("Nenhum processo usando diretamente esse perfil foi encontrado.")

    if platform.system().lower().startswith("win"):
        pw = find_playwright_chromium_processes_windows()
        print("")
        if pw:
            print("Processos Chromium/Playwright ainda abertos:")
            for proc in pw:
                print(f"  - PID {proc.get('ProcessId')} | {proc.get('Name')}")
        else:
            print("Nenhum processo Chromium/Playwright residual encontrado.")

    print("")
    print("Comando recomendado antes de tentar novamente:")
    print("  python src\\whatsapp_browser_automation.py unlock-profile --kill --remove-locks")


async def cmd_unlock_profile(args: argparse.Namespace) -> None:
    config = load_config()

    print(f"Perfil: {config.profile_dir}")

    processes = find_profile_processes(config.profile_dir)
    if processes:
        print("Processos usando o perfil:")
        for proc in processes:
            print(f"  - PID {proc.get('ProcessId')} | {proc.get('Name')}")
    else:
        print("Nenhum processo usando diretamente esse perfil foi encontrado.")

    total_killed = 0

    if args.kill and processes:
        killed = kill_processes(processes)
        total_killed += killed
        print(f"Processos do perfil encerrados: {killed}")

    if args.kill_playwright and platform.system().lower().startswith("win"):
        pw = find_playwright_chromium_processes_windows()
        if pw:
            print("Processos Chromium/Playwright encontrados:")
            for proc in pw:
                print(f"  - PID {proc.get('ProcessId')} | {proc.get('Name')}")
            killed_pw = kill_processes_windows(pw)
            total_killed += killed_pw
            print(f"Processos Chromium/Playwright encerrados: {killed_pw}")
        else:
            print("Nenhum processo Chromium/Playwright residual encontrado.")

    if args.remove_locks:
        removed = remove_lock_files(config.profile_dir)
        if removed:
            print("Resultado da remoção de locks:")
            for item in removed:
                print(f"  - {item}")
        else:
            print("Nenhum arquivo de lock foi removido.")

    print(f"Total de processos encerrados: {total_killed}")
    print("Pronto. Agora tente abrir o watch-current novamente.")


async def cmd_login(args: argparse.Namespace) -> None:
    config = load_config()
    playwright, context, page = await open_whatsapp(config)
    try:
        await wait_for_whatsapp_ready(page, config.ready_timeout)
        print("")
        print("Sessão aberta.")
        print("Não rode outro comando usando o mesmo perfil enquanto esta janela estiver aberta.")
        print("Pressione ENTER aqui apenas quando quiser fechar esta sessão.")
        input()
    finally:
        await context.close()
        await playwright.stop()


INSPECT_JS = """
() => {
    const isVisible = (el) => {
        const rect = el.getBoundingClientRect();
        const style = window.getComputedStyle(el);
        return rect.width > 0 &&
               rect.height > 0 &&
               style.visibility !== "hidden" &&
               style.display !== "none" &&
               rect.bottom >= 0 &&
               rect.right >= 0 &&
               rect.top <= window.innerHeight &&
               rect.left <= window.innerWidth;
    };

    const normalize = (txt) => (txt || "")
        .replace(/\\u200e/g, "")
        .replace(/\\u200f/g, "")
        .replace(/\\s+/g, " ")
        .trim()
        .slice(0, 500);

    const nodes = Array.from(document.querySelectorAll(
        "div, span, button, input, textarea, [role], [aria-label], [contenteditable='true'], [data-testid], [data-pre-plain-text]"
    ));

    return nodes
        .filter(isVisible)
        .map((el, index) => {
            const rect = el.getBoundingClientRect();
            return {
                index,
                tag: el.tagName.toLowerCase(),
                role: el.getAttribute("role"),
                aria_label: el.getAttribute("aria-label"),
                title: el.getAttribute("title"),
                data_testid: el.getAttribute("data-testid"),
                data_pre_plain_text: el.getAttribute("data-pre-plain-text"),
                contenteditable: el.getAttribute("contenteditable"),
                class_name: String(el.className || "").slice(0, 200),
                text: normalize(el.innerText || el.textContent || el.getAttribute("aria-label") || ""),
                x: Math.round(rect.x),
                y: Math.round(rect.y),
                width: Math.round(rect.width),
                height: Math.round(rect.height)
            };
        })
        .filter(item =>
            item.text ||
            item.role ||
            item.aria_label ||
            item.title ||
            item.data_testid ||
            item.data_pre_plain_text ||
            item.contenteditable
        );
}
"""


async def cmd_inspect(args: argparse.Namespace) -> None:
    config = load_config()
    playwright, context, page = await open_whatsapp(config)
    EXPORT_DIR.mkdir(parents=True, exist_ok=True)

    try:
        await wait_for_whatsapp_ready(page, config.ready_timeout)
        items: List[Dict[str, Any]] = await page.evaluate(INSPECT_JS)

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        json_path = EXPORT_DIR / f"whatsapp_dom_points_{timestamp}.json"
        csv_path = EXPORT_DIR / f"whatsapp_dom_points_{timestamp}.csv"

        with json_path.open("w", encoding="utf-8") as f:
            json.dump(items, f, ensure_ascii=False, indent=2)

        fields = [
            "index", "tag", "role", "aria_label", "title", "data_testid",
            "data_pre_plain_text", "contenteditable", "class_name", "text",
            "x", "y", "width", "height"
        ]
        with csv_path.open("w", encoding="utf-8", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=fields)
            writer.writeheader()
            for item in items:
                writer.writerow({k: item.get(k) for k in fields})

        print(f"Pontos visíveis encontrados: {len(items)}")
        print(f"JSON: {json_path}")
        print(f"CSV:  {csv_path}")
    finally:
        await context.close()
        await playwright.stop()


READ_MESSAGES_JS = """
() => {
    const normalize = (txt) => (txt || "")
        .replace(/\\u200e/g, "")
        .replace(/\\u200f/g, "")
        .replace(/\\s+/g, " ")
        .trim();

    const isVisible = (el) => {
        const rect = el.getBoundingClientRect();
        const style = window.getComputedStyle(el);
        return rect.width > 0 &&
               rect.height > 0 &&
               style.visibility !== "hidden" &&
               style.display !== "none" &&
               rect.bottom >= 0 &&
               rect.right >= 0 &&
               rect.top <= window.innerHeight &&
               rect.left <= window.innerWidth;
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
        if (!isVisible(el)) continue;

        const rect = el.getBoundingClientRect();
        const centerX = rect.x + rect.width / 2;

        // Evita capturar lista lateral de conversas.
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
            Math.round(rect.y)
        ].join("|");

        if (seen.has(key)) continue;
        seen.add(key);

        messages.push({
            direction,
            sender: meta.sender,
            timestamp_text: meta.timestamp_text,
            text,
            x: Math.round(rect.x),
            y: Math.round(rect.y),
            width: Math.round(rect.width),
            height: Math.round(rect.height)
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
            const rect = el.getBoundingClientRect();
            const style = window.getComputedStyle(el);
            return rect.width > window.innerWidth * 0.35 &&
                   rect.height > window.innerHeight * 0.30 &&
                   rect.x > window.innerWidth * 0.20 &&
                   el.scrollHeight > el.clientHeight + 100 &&
                   style.overflowY !== "hidden" &&
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


def message_hash(message: Dict[str, Any]) -> str:
    raw = json.dumps(
        {
            "direction": message.get("direction"),
            "sender": message.get("sender"),
            "timestamp_text": message.get("timestamp_text"),
            "text": message.get("text"),
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


async def read_current_messages(page: Page) -> List[Dict[str, Any]]:
    messages: List[Dict[str, Any]] = await page.evaluate(READ_MESSAGES_JS)
    now = datetime.now().isoformat(timespec="seconds")

    enriched = []
    for msg in messages:
        enriched.append({
            "captured_at": now,
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
            "hash": message_hash(msg),
        })
    return enriched


async def download_current_chat(page: Page, scrolls: int, delay: float) -> List[Dict[str, Any]]:
    all_by_hash: Dict[str, Dict[str, Any]] = {}

    for i in range(max(1, scrolls)):
        messages = await read_current_messages(page)
        for msg in messages:
            all_by_hash[msg["hash"]] = msg

        print(f"Rolagem {i + 1}/{scrolls}: mensagens únicas até agora = {len(all_by_hash)}")

        if i < scrolls - 1:
            await page.evaluate(SCROLL_CHAT_UP_JS)
            await page.wait_for_timeout(int(delay * 1000))

    return list(all_by_hash.values())


async def cmd_read_current(args: argparse.Namespace) -> None:
    config = load_config()
    playwright, context, page = await open_whatsapp(config)
    EXPORT_DIR.mkdir(parents=True, exist_ok=True)

    try:
        await wait_for_whatsapp_ready(page, config.ready_timeout)

        if not args.no_prompt:
            print("Abra manualmente o chat desejado no WhatsApp Web.")
            print("Quando estiver pronto, pressione ENTER aqui.")
            input()

        messages = await read_current_messages(page)

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        out_path = EXPORT_DIR / f"messages_current_visible_{timestamp}.json"

        with out_path.open("w", encoding="utf-8") as f:
            json.dump(messages, f, ensure_ascii=False, indent=2)

        print(f"Mensagens visíveis capturadas: {len(messages)}")
        print(f"Arquivo: {out_path}")

        for i, msg in enumerate(messages[-10:], start=1):
            print(f"{i:02d}. [{msg['direction']}] {msg.get('sender') or ''} {msg['text']}")
    finally:
        await context.close()
        await playwright.stop()


async def cmd_download_current(args: argparse.Namespace) -> None:
    config = load_config()
    playwright, context, page = await open_whatsapp(config)
    EXPORT_DIR.mkdir(parents=True, exist_ok=True)

    try:
        await wait_for_whatsapp_ready(page, config.ready_timeout)

        if not args.no_prompt:
            print("Abra manualmente o chat desejado no WhatsApp Web.")
            print("Quando estiver pronto, pressione ENTER aqui.")
            input()

        messages = await download_current_chat(
            page=page,
            scrolls=args.scrolls,
            delay=args.delay,
        )

        messages = sorted(
            messages,
            key=lambda m: (
                m.get("timestamp_text") or "",
                m.get("position", {}).get("y", 0),
                m.get("hash", ""),
            )
        )

        out_path = EXPORT_DIR / "messages_current_chat.json"
        timestamped = EXPORT_DIR / f"messages_current_chat_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"

        for path in [out_path, timestamped]:
            with path.open("w", encoding="utf-8") as f:
                json.dump(messages, f, ensure_ascii=False, indent=2)

        print(f"Mensagens capturadas no chat atual: {len(messages)}")
        print(f"Arquivo principal: {out_path}")
        print(f"Cópia datada:      {timestamped}")

    finally:
        await context.close()
        await playwright.stop()


async def cmd_watch_current(args: argparse.Namespace) -> None:
    config = load_config()
    playwright, context, page = await open_whatsapp(config)
    EXPORT_DIR.mkdir(parents=True, exist_ok=True)

    out_path = EXPORT_DIR / "messages_watch.jsonl"
    seen_hashes: set[str] = set()

    try:
        await wait_for_whatsapp_ready(page, config.ready_timeout)

        if not args.no_prompt:
            print("Abra manualmente o chat que deseja monitorar.")
            print("Quando estiver pronto, pressione ENTER aqui.")
            input()

        # Carrega estado inicial para evitar gravar tudo como se fosse novo.
        initial = await read_current_messages(page)
        for msg in initial:
            seen_hashes.add(msg["hash"])

        print(f"Estado inicial carregado com {len(seen_hashes)} mensagens visíveis.")
        print(f"Monitorando chat atual a cada {args.interval} segundos.")
        print(f"Saída: {out_path}")
        print("Pressione CTRL+C para encerrar.")

        while True:
            messages = await read_current_messages(page)
            new_messages = [m for m in messages if m["hash"] not in seen_hashes]

            if new_messages:
                with out_path.open("a", encoding="utf-8") as f:
                    for msg in new_messages:
                        seen_hashes.add(msg["hash"])
                        f.write(json.dumps(msg, ensure_ascii=False) + "\n")

                print(f"{len(new_messages)} mensagem(ns) nova(s) registrada(s). Total em memória: {len(seen_hashes)}")

            await asyncio.sleep(args.interval)

    except KeyboardInterrupt:
        print("Monitoramento encerrado.")
        print(f"Arquivo: {out_path}")
    finally:
        await context.close()
        await playwright.stop()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Automação autorizada do WhatsApp Web com Playwright."
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_doctor = sub.add_parser("doctor", help="Diagnostica perfil, locks e processos.")
    p_doctor.set_defaults(func=cmd_doctor)

    p_unlock = sub.add_parser("unlock-profile", help="Desbloqueia perfil, mata processos e remove locks órfãos.")
    p_unlock.add_argument("--kill", action="store_true", help="Encerra processos encontrados usando o perfil.")
    p_unlock.add_argument("--kill-playwright", action="store_true", help="Também encerra Chromiums residuais do Playwright.")
    p_unlock.add_argument("--remove-locks", action="store_true", help="Remove arquivos SingletonLock/Cookie/Socket do perfil.")
    p_unlock.set_defaults(func=cmd_unlock_profile)

    p_login = sub.add_parser("login", help="Abre o WhatsApp Web para login manual via QR Code.")
    p_login.set_defaults(func=cmd_login)

    p_inspect = sub.add_parser("inspect", help="Exporta pontos visíveis da interface.")
    p_inspect.set_defaults(func=cmd_inspect)

    p_read = sub.add_parser("read-current", help="Lê mensagens atualmente visíveis no chat aberto.")
    p_read.add_argument("--no-prompt", action="store_true", help="Não aguarda ENTER antes de ler.")
    p_read.set_defaults(func=cmd_read_current)

    p_down = sub.add_parser("download-current", help="Rola para cima e baixa mensagens do chat atual.")
    p_down.add_argument("--scrolls", type=int, default=30, help="Quantidade de rolagens para cima.")
    p_down.add_argument("--delay", type=float, default=1.2, help="Pausa entre rolagens em segundos.")
    p_down.add_argument("--no-prompt", action="store_true", help="Não aguarda ENTER antes de baixar.")
    p_down.set_defaults(func=cmd_download_current)

    p_watch = sub.add_parser("watch-current", help="Monitora o chat atual e salva mensagens novas.")
    p_watch.add_argument("--interval", type=int, default=5, help="Intervalo de leitura em segundos.")
    p_watch.add_argument("--no-prompt", action="store_true", help="Não aguarda ENTER antes de monitorar.")
    p_watch.set_defaults(func=cmd_watch_current)

    return parser


async def main_async() -> None:
    parser = build_parser()
    args = parser.parse_args()
    await args.func(args)


def main() -> None:
    asyncio.run(main_async())


if __name__ == "__main__":
    main()
