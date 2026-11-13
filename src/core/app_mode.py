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


def acr122u_allowed() -> bool:
    """Darf der USB-Leser ACR122U abgefragt werden? Im TonUINO-Modus immer (dort
    waehlt man ihn unter RFID-Leser), im ESPuino-Modus nur, wenn eingeschaltet."""
    if current_mode() != MODE_ESPUINO:
        return True
    from core.espuino_device import use_acr122u
    return use_acr122u()


def set_mode(mode: str) -> None:
    if mode not in MODES:
        raise ValueError(f"Unbekannter Modus: {mode!r}")
    from PyQt6.QtCore import QSettings
    QSettings().setValue(SETTINGS_APP_MODE, mode)


def needs_first_start_choice() -> bool:
    """True nur beim allerersten Start (noch gar keine Einstellungen): Dann fragt
    die App, ob sie fuer TonUINO oder ESPuino genutzt wird. Wer die App schon
    benutzt hat (Einstellungen vorhanden, aber noch kein Modus), bekommt
    stillschweigend TonUINO gespeichert und wird nie gefragt."""
    from PyQt6.QtCore import QSettings
    settings = QSettings()
    # macOS: ohne das liefert allKeys() auch globale Systemwerte (NSUserDefaults),
    # und die Frage kaeme nie
    settings.setFallbacksEnabled(False)
    if settings.contains(SETTINGS_APP_MODE):
        return False
    if settings.allKeys():
        settings.setValue(SETTINGS_APP_MODE, MODE_TONUINO)
        return False
    return True


def display_name(mode: str) -> str:
    return ESPUINO_APP_NAME if mode == MODE_ESPUINO else APP_NAME
