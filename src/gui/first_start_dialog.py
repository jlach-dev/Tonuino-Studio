"""
Frage beim allerersten Start: Wird die App fuer TonUINO oder fuer ESPuino
genutzt? (siehe core/app_mode.py, needs_first_start_choice). Eigene Datei des Forks.
"""

from typing import Optional

from PyQt6.QtWidgets import QMessageBox, QWidget

from core.app_mode import ESPUINO_APP_NAME, MODE_ESPUINO, MODE_TONUINO, set_mode
from core.branding import APP_NAME

TEXT = (
    "<p>Wofür möchtest du das Programm nutzen?</p>"
    "<p><b>TonUINO</b>: SD-Karte mit nummerierten Ordnern, RFID-Karten, Kartencover und Druckbogen.</p>"
    f"<p><b>ESPuino</b>: Karten direkt am ESPuino zuweisen (oder aus dem ESPuino-Mediahub lesen), "
    f"Kartencover und Druckbogen. Das Programm heißt dann „{ESPUINO_APP_NAME}“, die Funktionen für die "
    "TonUINO-SD-Karte und den RFID-Leser sind ausgeblendet.</p>"
    "<p>Die Bibliothek gibt es in beiden Fällen. Du kannst das später jederzeit unter "
    "<b>Einstellungen → Modus</b> ändern.</p>"
)


def build_dialog(parent: Optional[QWidget] = None) -> QMessageBox:
    box = QMessageBox(parent)
    box.setWindowTitle(f"Willkommen bei {APP_NAME}")
    box.setIcon(QMessageBox.Icon.Question)
    box.setText(TEXT)
    tonuino = box.addButton("TonUINO", QMessageBox.ButtonRole.AcceptRole)
    espuino = box.addButton("ESPuino", QMessageBox.ButtonRole.AcceptRole)
    box.setDefaultButton(tonuino)
    box.setEscapeButton(tonuino)  # Schliessen ohne Wahl = TonUINO, wie bisher
    box.setProperty("mode_buttons", {MODE_TONUINO: tonuino, MODE_ESPUINO: espuino})
    return box


def chosen_mode(box: QMessageBox) -> str:
    return MODE_ESPUINO if box.clickedButton() is box.property("mode_buttons")[MODE_ESPUINO] else MODE_TONUINO


def ask_app_mode(parent: Optional[QWidget] = None) -> str:
    """Fragt einmal und speichert die Wahl; liefert den Modus"""
    box = build_dialog(parent)
    box.exec()
    mode = chosen_mode(box)
    set_mode(mode)
    return mode
