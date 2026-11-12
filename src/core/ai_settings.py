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


def get_image_size() -> str:
    """Eingestellte Bildgroesse fuer KI-Bilder; unbekannte Werte fallen auf den Standard zurueck"""
    from PyQt6.QtCore import QSettings
    value = QSettings().value(SETTINGS_IMAGE_SIZE, DEFAULT_IMAGE_SIZE, type=str)
    return value if value in IMAGE_SIZES else DEFAULT_IMAGE_SIZE


def get_extend_prompt() -> str:
    """Gewaehlte Fassung des Prompts fuer "Rand per KI"; unbekannte Werte fallen auf den Standard zurueck"""
    from PyQt6.QtCore import QSettings
    value = QSettings().value(SETTINGS_EXTEND_PROMPT, DEFAULT_EXTEND_PROMPT, type=str)
    return value if value in EXTEND_PROMPTS else DEFAULT_EXTEND_PROMPT

KEYRING_SERVICE = "Tonuino-Manager"
KEYRING_USER = "gemini_api_key"
ENV_VARS = ("GEMINI_API_KEY", "GOOGLE_API_KEY")

# Modelle laut ai.google.dev (Stand 10/2026). Ueber die Einstellungen
# aenderbar, falls Google neue IDs einfuehrt.
DEFAULT_IMAGE_MODEL = "gemini-nano-banana-2.1"
DEFAULT_TEXT_MODEL = "gemini-3.5-flash-lite"

# Bildgroesse beim Anbieter: 1K misst rund 1260 x 850 px und reicht fuer den Druck
# (1060 x 690 px bei 300 dpi mit Anschnitt), 2K liefert rund 2530 x 1700 px und bis 7,6 MB je Bild
DEFAULT_IMAGE_SIZE = "1K"
IMAGE_SIZES = {
    "1K": "1K: rund 1260 px, reicht für den Druck",
    "2K": "2K: rund 2530 px, deutlich größere Dateien",
}
SETTINGS_IMAGE_SIZE = "covers/image_size"

# Prompt fuer "Rand per KI": die aktuelle Fassung verbietet einen eingesetzten Kasten, die bisherige
# (bis 12.11.2026, in 27 Testbildern bewaehrt) bleibt als Ausweichmoeglichkeit waehlbar
EXTEND_PROMPT_SEAMLESS, EXTEND_PROMPT_CLASSIC = "seamless", "classic"
DEFAULT_EXTEND_PROMPT = EXTEND_PROMPT_SEAMLESS
EXTEND_PROMPTS = {
    EXTEND_PROMPT_SEAMLESS: "Aktuell: ohne eingesetzten Kasten",
    EXTEND_PROMPT_CLASSIC: "Bisher (bis 12.11.2026)",
}
SETTINGS_EXTEND_PROMPT = "covers/extend_prompt"
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


# --- Spotify (Cover-Suche) -------------------------------------------------------------------
# Client-Credentials einer eigenen Spotify-App (developer.spotify.com/dashboard);
# ebenfalls im Schluesselbund bzw. als Umgebungsvariablen.

SPOTIFY_ID_USER = "spotify_client_id"
SPOTIFY_SECRET_USER = "spotify_client_secret"
SPOTIFY_ENV = ("SPOTIFY_CLIENT_ID", "SPOTIFY_CLIENT_SECRET")


def get_spotify_credentials() -> Optional[tuple]:
    """(client_id, client_secret) oder None"""
    client_id = os.environ.get(SPOTIFY_ENV[0], "").strip()
    secret = os.environ.get(SPOTIFY_ENV[1], "").strip()
    if client_id and secret:
        return client_id, secret
    try:
        import keyring
        client_id = keyring.get_password(KEYRING_SERVICE, SPOTIFY_ID_USER) or ""
        secret = keyring.get_password(KEYRING_SERVICE, SPOTIFY_SECRET_USER) or ""
    except Exception:
        log.exception("Spotify-Zugangsdaten konnten nicht gelesen werden")
        return None
    return (client_id, secret) if client_id and secret else None


def spotify_credentials_from_env() -> bool:
    return all(os.environ.get(name, "").strip() for name in SPOTIFY_ENV)


def set_spotify_credentials(client_id: str, secret: str):
    """Speichert die Zugangsdaten; beide leer = entfernen"""
    import keyring
    for user, value in ((SPOTIFY_ID_USER, client_id), (SPOTIFY_SECRET_USER, secret)):
        if value.strip():
            keyring.set_password(KEYRING_SERVICE, user, value.strip())
        else:
            try:
                keyring.delete_password(KEYRING_SERVICE, user)
            except keyring.errors.PasswordDeleteError:
                pass


# --- AcoustID (Stuecke erkennen) --------------------------------------------------------------
# Kostenloser Application-Key von https://acoustid.org/new-application (nicht-kommerziell)

ACOUSTID_USER = "acoustid_api_key"
ACOUSTID_ENV = "ACOUSTID_API_KEY"
SETTINGS_FPCALC = "library/fpcalc_path"


def get_acoustid_key() -> Optional[str]:
    value = os.environ.get(ACOUSTID_ENV, "").strip()
    if value:
        return value
    try:
        import keyring
        return keyring.get_password(KEYRING_SERVICE, ACOUSTID_USER) or None
    except Exception:
        log.exception("AcoustID-Key konnte nicht gelesen werden")
        return None


def acoustid_key_from_env() -> bool:
    return bool(os.environ.get(ACOUSTID_ENV, "").strip())


def set_acoustid_key(key: str):
    import keyring
    if key.strip():
        keyring.set_password(KEYRING_SERVICE, ACOUSTID_USER, key.strip())
    else:
        try:
            keyring.delete_password(KEYRING_SERVICE, ACOUSTID_USER)
        except keyring.errors.PasswordDeleteError:
            pass
