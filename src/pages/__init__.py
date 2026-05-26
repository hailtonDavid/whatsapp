"""Page Objects do WhatsApp Web."""

from pages.base_page import BasePage, SelectorGroup
from pages.whatsapp_login_page import WhatsAppLoginPage
from pages.whatsapp_main_page import WhatsAppMainPage

__all__ = [
    "BasePage",
    "SelectorGroup",
    "WhatsAppLoginPage",
    "WhatsAppMainPage",
]
