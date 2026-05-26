"""Seletores e constantes compartilhadas da automação WhatsApp Web."""

WA_URL = "https://web.whatsapp.com/"

# RF06 / tela de login (QR Code) — ordem = prioridade para RNF01
WHATSAPP_LOGIN_SELECTORS: tuple[str, ...] = (
    'canvas[aria-label*="QR" i]',
    'canvas[aria-label*="Scan" i]',
    '[data-testid="link-device-qrcode-alt-linking-help"]',
    '[data-testid="qrcode"]',
)

# Interface principal após login — fallbacks para RNF01
WHATSAPP_MAIN_SELECTORS: tuple[str, ...] = (
    "div[contenteditable='true'][data-tab='3']",
    "div[contenteditable='true'][role='textbox']",
    "[data-testid='chat-list']",
    "#pane-side",
    "[aria-label*='Pesquisar' i]",
    "[aria-label*='Search' i]",
)

WHATSAPP_SEARCH_SELECTORS: tuple[str, ...] = (
    "[aria-label*='Pesquisar' i]",
    "[aria-label*='Search' i]",
    "div[contenteditable='true'][data-tab='3']",
)

WHATSAPP_MESSAGE_BOX_SELECTORS: tuple[str, ...] = (
    "div[contenteditable='true'][data-tab='10']",
    "footer div[contenteditable='true']",
    "div[contenteditable='true'][role='textbox']",
)

WHATSAPP_LOGIN_SELECTOR = ", ".join(WHATSAPP_LOGIN_SELECTORS)
