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
    QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFormLayout, QHBoxLayout, QLabel, QLineEdit, QMessageBox,
    QPushButton, QVBoxLayout,
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
        self.setWindowTitle("KI & Online-Dienste")
        self.setMinimumWidth(520)
        layout = QVBoxLayout(self)

        info = QLabel(
            'Den API-Key erhältst du im <a href="https://aistudio.google.com/apikey">Google AI Studio</a>. '
            "Bildgenerierung ist kostenpflichtig (Abrechnung im Google-Cloud-Projekt). "
            "Der Key wird im Schlüsselbund des Betriebssystems gespeichert.<br><br>"
            "<b>Datenschutz:</b> Für KI-Kartencover gehen Titel, Tags, die Recherche-Ergebnisse sowie das "
            "Album-Cover und gewählte Vorlagen als Referenzbilder an Google (Gemini API). Es gelten die "
            "Bedingungen von Google für die Gemini API.")
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
        self.image_size_combo = QComboBox()
        for size, label in ai_settings.IMAGE_SIZES.items():
            self.image_size_combo.addItem(label, size)
        self.image_size_combo.setCurrentIndex(self.image_size_combo.findData(ai_settings.get_image_size()))
        form.addRow("Bildgröße:", self.image_size_combo)
        self.extend_prompt_combo = QComboBox()
        for key, label in ai_settings.EXTEND_PROMPTS.items():
            self.extend_prompt_combo.addItem(label, key)
        self.extend_prompt_combo.setCurrentIndex(self.extend_prompt_combo.findData(ai_settings.get_extend_prompt()))
        self.extend_prompt_combo.setToolTip("Fassung des Prompts für „Rand per KI“. Die bisherige bleibt als "
                                            "Ausweichmöglichkeit, falls die aktuelle schlechter abschneidet.")
        form.addRow("Rand-Prompt:", self.extend_prompt_combo)
        self.text_model_edit = QLineEdit(text_model())
        form.addRow("Recherche-Modell:", self.text_model_edit)
        layout.addLayout(form)

        spotify_info = QLabel(
            'Optional für die Cover-Suche: Client-ID und Secret einer eigenen App aus dem '
            '<a href="https://developer.spotify.com/dashboard">Spotify Developer Dashboard</a>.')
        spotify_info.setOpenExternalLinks(True)
        spotify_info.setWordWrap(True)
        layout.addWidget(spotify_info)
        spotify_form = QFormLayout()
        self.spotify_id_edit = QLineEdit()
        self.spotify_secret_edit = QLineEdit()
        self.spotify_secret_edit.setEchoMode(QLineEdit.EchoMode.Password)
        if ai_settings.spotify_credentials_from_env():
            for edit in (self.spotify_id_edit, self.spotify_secret_edit):
                edit.setPlaceholderText("aus Umgebungsvariable")
                edit.setEnabled(False)
        elif ai_settings.get_spotify_credentials():
            self.spotify_id_edit.setPlaceholderText("gespeichert – zum Ändern neu eingeben")
            self.spotify_secret_edit.setPlaceholderText("gespeichert – zum Ändern neu eingeben")
        spotify_form.addRow("Spotify Client-ID:", self.spotify_id_edit)
        spotify_form.addRow("Spotify Client-Secret:", self.spotify_secret_edit)
        layout.addLayout(spotify_form)

        acoustid_info = QLabel(
            'Optional zum Erkennen unklarer Stücke (Bibliothek → Album → „Stücke erkennen“): ein kostenloser '
            '<a href="https://acoustid.org/new-application">AcoustID-Application-Key</a> und das Programm '
            '<b>fpcalc</b> aus <a href="https://acoustid.org/chromaprint">Chromaprint</a>.')
        acoustid_info.setOpenExternalLinks(True)
        acoustid_info.setWordWrap(True)
        layout.addWidget(acoustid_info)
        acoustid_form = QFormLayout()
        self.acoustid_edit = QLineEdit()
        self.acoustid_edit.setEchoMode(QLineEdit.EchoMode.Password)
        if ai_settings.acoustid_key_from_env():
            self.acoustid_edit.setPlaceholderText("aus Umgebungsvariable ACOUSTID_API_KEY")
            self.acoustid_edit.setEnabled(False)
        elif ai_settings.get_acoustid_key():
            self.acoustid_edit.setPlaceholderText("gespeichert – zum Ändern neu eingeben")
        acoustid_form.addRow("AcoustID-Key:", self.acoustid_edit)
        fpcalc_row = QHBoxLayout()
        self.fpcalc_edit = QLineEdit(QSettings().value(ai_settings.SETTINGS_FPCALC, "", type=str))
        self.fpcalc_edit.setPlaceholderText("leer = automatisch (PATH oder Datenordner/tools)")
        fpcalc_row.addWidget(self.fpcalc_edit, 1)
        btn_fpcalc_pick = QPushButton("Wählen …")
        btn_fpcalc_pick.setObjectName("ghostButton")
        btn_fpcalc_pick.clicked.connect(self._pick_fpcalc)
        fpcalc_row.addWidget(btn_fpcalc_pick)
        btn_fpcalc_download = QPushButton("Herunterladen …")
        btn_fpcalc_download.setObjectName("ghostButton")
        btn_fpcalc_download.setToolTip("Offizielles Chromaprint-Release von GitHub in den Datenordner (tools/) laden")
        btn_fpcalc_download.clicked.connect(self._download_fpcalc)
        fpcalc_row.addWidget(btn_fpcalc_download)
        acoustid_form.addRow("fpcalc:", fpcalc_row)
        self.fpcalc_status = QLabel()
        self.fpcalc_status.setObjectName("folderMeta")
        self.fpcalc_status.setWordWrap(True)
        acoustid_form.addRow("", self.fpcalc_status)
        layout.addLayout(acoustid_form)
        self._update_fpcalc_status()

        self.remove_key_check = QCheckBox("Gespeicherte Zugangsdaten (Gemini, Spotify, AcoustID) entfernen")
        layout.addWidget(self.remove_key_check)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Speichern")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("Abbrechen")
        buttons.accepted.connect(self._save)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _data_dir(self) -> str:
        return QSettings().value("library/data_dir", "", type=str)

    def _update_fpcalc_status(self):
        from core.acoustid import find_fpcalc
        found = find_fpcalc(self.fpcalc_edit.text().strip(), self._data_dir())
        self.fpcalc_status.setText(f"fpcalc gefunden: {found}" if found
                                   else "fpcalc nicht gefunden – herunterladen oder Pfad wählen")

    def _pick_fpcalc(self):
        from PyQt6.QtWidgets import QFileDialog
        path, _ = QFileDialog.getOpenFileName(self, "fpcalc wählen", "", "fpcalc (fpcalc*);;Alle (*)")
        if path:
            self.fpcalc_edit.setText(path)
            self._update_fpcalc_status()

    def _download_fpcalc(self):
        from core.acoustid import FPCALC_VERSION, download_fpcalc
        from PyQt6.QtWidgets import QApplication
        from PyQt6.QtCore import Qt
        data_dir = self._data_dir()
        if not data_dir:
            QMessageBox.information(self, "Datenordner", "Bitte zuerst den Datenordner der Bibliothek festlegen "
                                                         "(Cover-Manager öffnen oder Bibliothek → Ordner …).")
            return
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            path = download_fpcalc(data_dir)
        except Exception as e:
            QApplication.restoreOverrideCursor()
            QMessageBox.warning(self, "Download fehlgeschlagen", str(e))
            return
        QApplication.restoreOverrideCursor()
        self.fpcalc_edit.setText(path)
        self._update_fpcalc_status()
        QMessageBox.information(self, "fpcalc", f"Chromaprint {FPCALC_VERSION} (fpcalc) wurde nach\n{path}\ngeladen.")

    def _save(self):
        settings = QSettings()
        settings.setValue(ai_settings.SETTINGS_FPCALC, self.fpcalc_edit.text().strip())
        settings.setValue(ai_settings.SETTINGS_IMAGE_SIZE, self.image_size_combo.currentData())
        settings.setValue(ai_settings.SETTINGS_EXTEND_PROMPT, self.extend_prompt_combo.currentData())
        settings.setValue(ai_settings.SETTINGS_IMAGE_MODEL,
                          self.image_model_edit.text().strip() or ai_settings.DEFAULT_IMAGE_MODEL)
        settings.setValue(ai_settings.SETTINGS_TEXT_MODEL,
                          self.text_model_edit.text().strip() or ai_settings.DEFAULT_TEXT_MODEL)
        try:
            if self.remove_key_check.isChecked():
                ai_settings.set_api_key("")
                ai_settings.set_spotify_credentials("", "")
                ai_settings.set_acoustid_key("")
            else:
                if self.key_edit.text().strip():
                    ai_settings.set_api_key(self.key_edit.text())
                if self.acoustid_edit.text().strip():
                    ai_settings.set_acoustid_key(self.acoustid_edit.text())
                spotify_id = self.spotify_id_edit.text().strip()
                spotify_secret = self.spotify_secret_edit.text().strip()
                if spotify_id and spotify_secret:
                    ai_settings.set_spotify_credentials(spotify_id, spotify_secret)
                elif spotify_id or spotify_secret:
                    QMessageBox.information(self, "Spotify", "Bitte Client-ID und Secret gemeinsam eingeben.")
                    return
        except Exception as e:
            QMessageBox.warning(self, "Schlüsselbund", f"API-Key konnte nicht gespeichert werden: {e}")
            return
        self.accept()
