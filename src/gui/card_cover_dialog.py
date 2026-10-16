"""
KI-Einstellungen (Gemini) und gemeinsame Helfer fuer den Cover-Manager
(siehe gui/cover_manager_dialog.py)
"""

import io
import logging
from typing import Optional

from PIL import Image
from PyQt6.QtCore import QSettings
from PyQt6.QtGui import QPixmap
from PyQt6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFormLayout, QLabel, QLineEdit, QMessageBox, QVBoxLayout,
)

from core import ai_settings
from core.card_layout import ORIENTATION_LANDSCAPE, ORIENTATION_PORTRAIT
from core.cover_research import make_client

log = logging.getLogger(__name__)

ORIENTATIONS = [("Hochformat", ORIENTATION_PORTRAIT), ("Querformat", ORIENTATION_LANDSCAPE)]


def pil_to_pixmap(image: Image.Image) -> QPixmap:
    buffer = io.BytesIO()
    image.save(buffer, "PNG")
    pixmap = QPixmap()
    pixmap.loadFromData(buffer.getvalue(), "PNG")
    return pixmap


def bytes_to_pixmap(data: Optional[bytes]) -> Optional[QPixmap]:
    if not data:
        return None
    pixmap = QPixmap()
    return pixmap if pixmap.loadFromData(data) else None


def gemini_client():
    """Gemini-Client oder None, wenn kein API-Key hinterlegt ist"""
    key = ai_settings.get_api_key()
    if not key:
        return None
    try:
        return make_client(key)
    except Exception:
        log.exception("Gemini-Client konnte nicht erstellt werden")
        return None


def image_model() -> str:
    return QSettings().value(ai_settings.SETTINGS_IMAGE_MODEL, ai_settings.DEFAULT_IMAGE_MODEL, type=str)


def text_model() -> str:
    return QSettings().value(ai_settings.SETTINGS_TEXT_MODEL, ai_settings.DEFAULT_TEXT_MODEL, type=str)


def _select_data(combo: QComboBox, value):
    index = combo.findData(value)
    if index >= 0:
        combo.setCurrentIndex(index)


class AiSettingsDialog(QDialog):
    """Gemini-API-Key (Schluesselbund) und Modell-IDs"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("KI-Einstellungen (Gemini)")
        self.setMinimumWidth(520)
        layout = QVBoxLayout(self)

        info = QLabel(
            'Den API-Key erhältst du im <a href="https://aistudio.google.com/apikey">Google AI Studio</a>. '
            "Bildgenerierung ist kostenpflichtig (Abrechnung im Google-Cloud-Projekt). "
            "Der Key wird im Schlüsselbund des Betriebssystems gespeichert.")
        info.setOpenExternalLinks(True)
        info.setWordWrap(True)
        layout.addWidget(info)

        form = QFormLayout()
        self.key_edit = QLineEdit()
        self.key_edit.setEchoMode(QLineEdit.EchoMode.Password)
        if ai_settings.api_key_from_env():
            self.key_edit.setPlaceholderText("wird aus Umgebungsvariable GEMINI_API_KEY gelesen")
            self.key_edit.setEnabled(False)
        elif ai_settings.get_api_key():
            self.key_edit.setPlaceholderText("gespeichert – zum Ändern neu eingeben")
        form.addRow("API-Key:", self.key_edit)

        self.image_model_edit = QLineEdit(image_model())
        form.addRow("Bildmodell:", self.image_model_edit)
        self.text_model_edit = QLineEdit(text_model())
        form.addRow("Recherche-Modell:", self.text_model_edit)
        layout.addLayout(form)

        self.remove_key_check = QCheckBox("Gespeicherten API-Key entfernen")
        layout.addWidget(self.remove_key_check)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Speichern")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("Abbrechen")
        buttons.accepted.connect(self._save)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _save(self):
        settings = QSettings()
        settings.setValue(ai_settings.SETTINGS_IMAGE_MODEL,
                          self.image_model_edit.text().strip() or ai_settings.DEFAULT_IMAGE_MODEL)
        settings.setValue(ai_settings.SETTINGS_TEXT_MODEL,
                          self.text_model_edit.text().strip() or ai_settings.DEFAULT_TEXT_MODEL)
        try:
            if self.remove_key_check.isChecked():
                ai_settings.set_api_key("")
            elif self.key_edit.text().strip():
                ai_settings.set_api_key(self.key_edit.text())
        except Exception as e:
            QMessageBox.warning(self, "Schlüsselbund", f"API-Key konnte nicht gespeichert werden: {e}")
            return
        self.accept()
