"""
Betriebsart der App: TonUINO (SD-Karte, RFID) oder ESPuino (Karten aus dem
ESPuino-Mediahub, keine SD-Karte). Der Modus steht in den Einstellungen und
wirkt nach einem Neustart; der Anzeigename wechselt mit ("ESPuino-Studio").

Eigene Datei des Forks (siehe core/branding.py).
"""

from core.branding import APP_NAME

SETTINGS_APP_MODE = "app/mode"
MODE_TONUINO = "tonuino"
MODE_ESPUINO = "espuino"
MODES = (MODE_TONUINO, MODE_ESPUINO)
ESPUINO_APP_NAME = "ESPuino-Studio"

MODE_LABELS = {MODE_TONUINO: "TonUINO", MODE_ESPUINO: "ESPuino"}


def current_mode() -> str:
    """Gespeicherter Modus; unbekannte Werte gelten als TonUINO"""
    from PyQt6.QtCore import QSettings
    value = QSettings().value(SETTINGS_APP_MODE, MODE_TONUINO, type=str)
    return value if value in MODES else MODE_TONUINO


def set_mode(mode: str) -> None:
    if mode not in MODES:
        raise ValueError(f"Unbekannter Modus: {mode!r}")
    from PyQt6.QtCore import QSettings
    QSettings().setValue(SETTINGS_APP_MODE, mode)


def display_name(mode: str) -> str:
    return ESPUINO_APP_NAME if mode == MODE_ESPUINO else APP_NAME
