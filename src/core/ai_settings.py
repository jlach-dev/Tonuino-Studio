"""
Einstellungen fuer die KI-Kartencover (Gemini): API-Key und Modelle

Der API-Key wird nie im Klartext in den QSettings abgelegt: zuerst die
Umgebungsvariable GEMINI_API_KEY (bzw. GOOGLE_API_KEY), sonst der
Schluesselbund des Betriebssystems (keyring: Windows-Anmeldeinformationen,
macOS-Schluesselbund, Secret Service unter Linux).
"""

import logging
import os
from typing import Optional

log = logging.getLogger(__name__)

KEYRING_SERVICE = "Tonuino-Manager"
KEYRING_USER = "gemini_api_key"
ENV_VARS = ("GEMINI_API_KEY", "GOOGLE_API_KEY")

# Modelle laut ai.google.dev (Stand 10/2026). Ueber die Einstellungen
# aenderbar, falls Google neue IDs einfuehrt.
DEFAULT_IMAGE_MODEL = "gemini-nano-banana-2.1"
DEFAULT_TEXT_MODEL = "gemini-3.5-flash-lite"

SETTINGS_IMAGE_MODEL = "covers/image_model"
SETTINGS_TEXT_MODEL = "covers/text_model"
SETTINGS_ORIENTATION = "covers/default_orientation"
SETTINGS_STYLE = "covers/default_style"
SETTINGS_TEMPLATE = "covers/label_template"
SETTINGS_OFFSET_X = "covers/offset_x_mm"
SETTINGS_OFFSET_Y = "covers/offset_y_mm"


def api_key_from_env() -> Optional[str]:
    for name in ENV_VARS:
        value = os.environ.get(name, "").strip()
        if value:
            return value
    return None


def get_api_key() -> Optional[str]:
    key = api_key_from_env()
    if key:
        return key
    try:
        import keyring
        return keyring.get_password(KEYRING_SERVICE, KEYRING_USER) or None
    except Exception:
        log.exception("API-Key konnte nicht aus dem Schluesselbund gelesen werden")
        return None


def set_api_key(key: str):
    """Speichert (oder loescht bei leerem key) den API-Key im Schluesselbund"""
    import keyring
    if key.strip():
        keyring.set_password(KEYRING_SERVICE, KEYRING_USER, key.strip())
    else:
        try:
            keyring.delete_password(KEYRING_SERVICE, KEYRING_USER)
        except keyring.errors.PasswordDeleteError:
            pass
