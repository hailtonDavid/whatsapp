"""Seletores e constantes compartilhadas da automação WhatsApp Web."""

WA_URL = "https://web.whatsapp.com/"

# RF06 / tela de login (QR Code) — ordem = prioridade para RNF01
WHATSAPP_LOGIN_SELECTORS: tuple[str, ...] = (
    'canvas[aria-label*="QR" i]',
    'canvas[aria-label*="Scan" i]',
    '[data-testid="link-device-qrcode-alt-linking-help"]',
    '[data-testid="qrcode"]',
)

# Interface principal após login — fallbacks para RNF01 (inclui WhatsApp Business)
WHATSAPP_MAIN_SELECTORS: tuple[str, ...] = (
    "#pane-side",
    "[data-testid='chat-list']",
    "[data-testid='chat-list-search']",
    "[data-testid='cell-frame-container']",
    "div[contenteditable='true'][data-tab='3']",
    "div[contenteditable='true'][role='textbox']",
    "[aria-label*='Pesquisar' i]",
    "[aria-label*='Search' i]",
    "[aria-label*='conversa' i]",
    "[title='Chats']",
    "[title='Conversas']",
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

WHATSAPP_ATTACH_BUTTON_SELECTORS: tuple[str, ...] = (
    '[data-testid="clip"]',
    'span[data-icon="clip"]',
    'span[data-icon="plus"]',
    'button[aria-label*="Anexar" i]',
    'button[aria-label*="Attach" i]',
    'div[role="button"][aria-label*="Anexar" i]',
    'div[role="button"][aria-label*="Attach" i]',
)

WHATSAPP_ATTACH_DOCUMENT_SELECTORS: tuple[str, ...] = (
    '[data-testid="mi-attach-document"]',
    'input[type="file"][accept*="*"]',
    'li[aria-label*="Documento" i]',
    'li[aria-label*="Document" i]',
    'span[data-icon="document"]',
)

WHATSAPP_ATTACH_MEDIA_SELECTORS: tuple[str, ...] = (
    '[data-testid="mi-attach-image"]',
    '[data-testid="mi-attach-media"]',
    'input[type="file"][accept*="image"]',
    'li[aria-label*="Fotos" i]',
    'li[aria-label*="Photos" i]',
    'span[data-icon="image"]',
)

WHATSAPP_LOGIN_SELECTOR = ", ".join(WHATSAPP_LOGIN_SELECTORS)
