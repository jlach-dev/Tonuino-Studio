"""
Dialoge fuer KI-Kartencover: Cover erzeugen (pro Ordner) und KI-Einstellungen
"""

import io
import logging
from typing import List, Optional

from PIL import Image
from PyQt6.QtCore import Qt, QSettings, QSize, QTimer
from PyQt6.QtGui import QIcon, QPixmap
from PyQt6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFormLayout, QHBoxLayout, QLabel,
    QLineEdit, QListWidget, QListWidgetItem, QMessageBox, QPlainTextEdit, QPushButton,
    QSpinBox, QVBoxLayout,
)

from core import ai_settings, card_store
from core.card_layout import ORIENTATION_LANDSCAPE, ORIENTATION_PORTRAIT
from core.cover_generator import (
    DEFAULT_STYLE, ESTIMATED_COST_PER_IMAGE, SOURCE_AI, SOURCE_BLUR, STYLE_PRESETS, GenerationRequest,
)
from core.cover_research import CoverBrief, FolderInfo, make_client
from core.sd_card import Folder
from gui.workers import CoverGenerateWorker, CoverResearchWorker

log = logging.getLogger(__name__)

ORIENTATIONS = [("Hochformat", ORIENTATION_PORTRAIT), ("Querformat", ORIENTATION_LANDSCAPE)]
SOURCES = [("KI-Illustration (Gemini)", SOURCE_AI), ("Ambient Blur aus Album-Cover (ohne KI)", SOURCE_BLUR)]


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


class CardCoverDialog(QDialog):
    """Kartencover fuer einen Ordner: recherchieren, Motive erzeugen, Titel
    setzen, speichern. Titel/Ausrichtung/Nummer aendern rendert nur neu
    (ohne API-Aufruf)."""

    PREVIEW_HEIGHT = 380

    def __init__(self, folder: Folder, folder_name: str, metadata_manager, parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"Kartencover – Ordner {folder.index:02d}")
        self.setMinimumSize(980, 640)
        self.folder = folder
        self.metadata_manager = metadata_manager
        self.client = gemini_client()

        self.info = FolderInfo(album=folder_name)
        self.brief = CoverBrief()
        self.variants: List[Image.Image] = []
        self.current_art: Optional[Image.Image] = None
        self._research_worker: Optional[CoverResearchWorker] = None
        self._generate_worker: Optional[CoverGenerateWorker] = None
        self.saved = False

        self._setup_ui()
        self._load_existing(folder_name)
        self._update_buttons()
        # Recherche direkt starten - ohne API-Key nur Tags + Cover-Suche
        QTimer.singleShot(0, self._start_research)

    # --- UI -------------------------------------------------------------------------

    def _setup_ui(self):
        layout = QHBoxLayout(self)
        layout.setSpacing(20)

        left = QVBoxLayout()
        self.preview = QLabel("Noch kein Motiv")
        self.preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.preview.setMinimumSize(self.PREVIEW_HEIGHT, self.PREVIEW_HEIGHT)
        self.preview.setStyleSheet("border: 1px dashed palette(mid); border-radius: 8px;")
        left.addWidget(self.preview, 1)

        left.addWidget(QLabel("Varianten (anklicken zum Auswählen):"))
        self.variant_list = QListWidget()
        self.variant_list.setViewMode(QListWidget.ViewMode.IconMode)
        self.variant_list.setFlow(QListWidget.Flow.LeftToRight)
        self.variant_list.setWrapping(False)
        self.variant_list.setIconSize(QSize(90, 90))
        self.variant_list.setFixedHeight(120)
        self.variant_list.currentRowChanged.connect(self._on_variant_selected)
        left.addWidget(self.variant_list)
        layout.addLayout(left, 1)

        right = QVBoxLayout()
        form = QFormLayout()

        self.title_edit = QLineEdit()
        self.title_edit.textChanged.connect(self._render_preview)
        form.addRow("Titel auf der Karte:", self.title_edit)

        self.orientation_combo = QComboBox()
        for label, value in ORIENTATIONS:
            self.orientation_combo.addItem(label, value)
        _select_data(self.orientation_combo,
                     QSettings().value(ai_settings.SETTINGS_ORIENTATION, ORIENTATION_PORTRAIT, type=str))
        self.orientation_combo.currentIndexChanged.connect(self._render_preview)
        form.addRow("Ausrichtung:", self.orientation_combo)

        self.number_check = QCheckBox("Ordnernummer klein oben rechts anzeigen")
        self.number_check.setChecked(True)
        self.number_check.toggled.connect(self._render_preview)
        form.addRow("", self.number_check)

        self.source_combo = QComboBox()
        for label, value in SOURCES:
            self.source_combo.addItem(label, value)
        self.source_combo.currentIndexChanged.connect(self._update_buttons)
        form.addRow("Motiv:", self.source_combo)

        self.style_combo = QComboBox()
        self.style_combo.addItems(list(STYLE_PRESETS))
        self.style_combo.setCurrentText(QSettings().value(ai_settings.SETTINGS_STYLE, DEFAULT_STYLE, type=str))
        form.addRow("Stil:", self.style_combo)

        self.extra_edit = QLineEdit()
        self.extra_edit.setPlaceholderText("optional, z.B. „mit Dinosaurier im Weltall“")
        form.addRow("Eigener Wunsch:", self.extra_edit)

        self.reference_label = QLabel()
        self.reference_label.setFixedSize(64, 64)
        self.reference_label.setScaledContents(True)
        self.reference_check = QCheckBox("Album-Cover als Vorlage an die KI geben")
        self.reference_check.setChecked(True)
        reference_row = QHBoxLayout()
        reference_row.addWidget(self.reference_label)
        reference_row.addWidget(self.reference_check, 1)
        form.addRow("Vorlage:", reference_row)

        right.addLayout(form)

        research_header = QHBoxLayout()
        research_header.addWidget(QLabel("Recherche (bearbeitbar, fließt in den Bild-Prompt):"), 1)
        self.btn_research = QPushButton("Neu recherchieren")
        self.btn_research.setObjectName("ghostButton")
        self.btn_research.clicked.connect(self._start_research)
        research_header.addWidget(self.btn_research)
        right.addLayout(research_header)

        self.research_edit = QPlainTextEdit()
        self.research_edit.setPlaceholderText("Recherche läuft …")
        right.addWidget(self.research_edit, 1)

        self.sources_label = QLabel()
        self.sources_label.setOpenExternalLinks(True)
        self.sources_label.setWordWrap(True)
        self.sources_label.setTextFormat(Qt.TextFormat.RichText)
        right.addWidget(self.sources_label)

        generate_row = QHBoxLayout()
        generate_row.addWidget(QLabel("Varianten:"))
        self.count_spin = QSpinBox()
        self.count_spin.setRange(1, 4)
        self.count_spin.setValue(2)
        generate_row.addWidget(self.count_spin)
        generate_row.addStretch(1)
        self.btn_generate = QPushButton("Motive erzeugen")
        self.btn_generate.setObjectName("primaryButton")
        self.btn_generate.clicked.connect(self._start_generation)
        generate_row.addWidget(self.btn_generate)
        right.addLayout(generate_row)

        self.status_label = QLabel()
        self.status_label.setWordWrap(True)
        right.addWidget(self.status_label)

        buttons = QDialogButtonBox()
        self.btn_save = buttons.addButton("Auf SD-Karte speichern", QDialogButtonBox.ButtonRole.AcceptRole)
        self.btn_delete = buttons.addButton("Kartencover löschen", QDialogButtonBox.ButtonRole.DestructiveRole)
        buttons.addButton("Schließen", QDialogButtonBox.ButtonRole.RejectRole)
        self.btn_save.clicked.connect(self._save)
        self.btn_delete.clicked.connect(self._delete)
        buttons.rejected.connect(self.reject)
        right.addWidget(buttons)

        layout.addLayout(right, 1)

        if self.client is None:
            self.status_label.setText(
                "Kein Gemini-API-Key hinterlegt (Menü Kartencover → KI-Einstellungen). "
                "Ohne Key ist nur „Ambient Blur“ aus dem Album-Cover möglich.")
            _select_data(self.source_combo, SOURCE_BLUR)

    def _load_existing(self, folder_name: str):
        meta = card_store.load_meta(self.folder.path)
        art = card_store.load_raw_art(self.folder.path)
        if meta:
            self.title_edit.setText(meta.title)
            _select_data(self.orientation_combo, meta.orientation)
            self.number_check.setChecked(meta.show_number)
            if meta.style:
                self.style_combo.setCurrentText(meta.style)
        else:
            self.title_edit.setText(folder_name)
        if art is not None:
            self._add_variant(art)

    # --- Recherche --------------------------------------------------------------------

    def _start_research(self):
        if self._research_worker is not None and self._research_worker.isRunning():
            return
        self.status_label.setText("Recherche läuft …")
        self._research_worker = CoverResearchWorker(
            self.folder, self.metadata_manager, self.client, text_model(), self.extra_edit.text())
        self._research_worker.completed.connect(self._on_research_done)
        self._research_worker.failed.connect(lambda msg: self.status_label.setText(f"Recherche fehlgeschlagen: {msg}"))
        self._research_worker.finished.connect(self._update_buttons)
        self._research_worker.start()
        self._update_buttons()

    def _on_research_done(self, info: FolderInfo, brief: CoverBrief):
        self.info = info
        self.brief = brief
        if not self.title_edit.text().strip() or self.title_edit.text().startswith("Ordner "):
            self.title_edit.setText(info.album or self.title_edit.text())
        self.research_edit.setPlainText(brief.as_text())

        pixmap = bytes_to_pixmap(brief.reference_image)
        self.reference_label.setPixmap(pixmap or QPixmap())
        self.reference_check.setEnabled(pixmap is not None)
        self.reference_label.setToolTip(f"Quelle: {brief.reference_source}" if pixmap else "kein Cover gefunden")

        if brief.sources:
            links = " · ".join(f'<a href="{url}">{i}</a>' for i, url in enumerate(brief.sources, start=1))
            self.sources_label.setText(f"Quellen: {links}")
        else:
            self.sources_label.setText("")
        self.status_label.setText("Recherche fertig." + ("" if pixmap else " Kein Album-Cover gefunden."))

    # --- Generieren -------------------------------------------------------------------

    def _start_generation(self):
        source = self.source_combo.currentData()
        count = self.count_spin.value() if source == SOURCE_AI else 1
        if source == SOURCE_AI and self.client is None:
            QMessageBox.information(self, "Kein API-Key", "Bitte zuerst einen Gemini-API-Key hinterlegen.")
            return
        request = GenerationRequest(
            info=self.info, brief=self.brief, brief_text=self.research_edit.toPlainText(),
            style=self.style_combo.currentText(), extra=self.extra_edit.text(),
            orientation=self.orientation_combo.currentData(),
            use_reference=self.reference_check.isChecked() and self.reference_check.isEnabled(),
        )
        if source == SOURCE_AI:
            self.status_label.setText(
                f"Erzeuge {count} Motiv(e) mit {image_model()} … (ca. {count * ESTIMATED_COST_PER_IMAGE:.2f} USD)")
        self._generate_worker = CoverGenerateWorker(source, request, self.client, image_model(), count)
        self._generate_worker.variant_ready.connect(self._add_variant)
        self._generate_worker.failed.connect(
            lambda msg: QMessageBox.warning(self, "Motiv konnte nicht erzeugt werden", msg))
        self._generate_worker.completed.connect(lambda: self.status_label.setText("Fertig – Variante auswählen."))
        self._generate_worker.finished.connect(self._update_buttons)
        self._generate_worker.start()
        self._update_buttons()

    def _add_variant(self, art: Image.Image):
        self.variants.append(art)
        thumb = art.copy()
        thumb.thumbnail((180, 180))
        item = QListWidgetItem(QIcon(pil_to_pixmap(thumb)), "")
        self.variant_list.addItem(item)
        self.variant_list.setCurrentRow(len(self.variants) - 1)

    def _on_variant_selected(self, row: int):
        self.current_art = self.variants[row] if 0 <= row < len(self.variants) else None
        self._render_preview()
        self._update_buttons()

    # --- Vorschau / Speichern ---------------------------------------------------------

    def _meta(self) -> card_store.CardMeta:
        source = self.source_combo.currentData()
        return card_store.CardMeta(
            title=self.title_edit.text().strip(),
            orientation=self.orientation_combo.currentData(),
            show_number=self.number_check.isChecked(),
            source=source,
            style=self.style_combo.currentText() if source == SOURCE_AI else "",
            prompt=self.extra_edit.text(),
            model=image_model() if source == SOURCE_AI else "",
            research_sources=list(self.brief.sources),
        )

    def _render_preview(self):
        if self.current_art is None:
            return
        card = card_store.render_from_meta(self.current_art, self._meta(), self.folder.index)
        pixmap = pil_to_pixmap(card)
        self.preview.setPixmap(pixmap.scaled(
            self.preview.size() - QSize(16, 16), Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation))

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._render_preview()

    def _update_buttons(self, *_):
        researching = self._research_worker is not None and self._research_worker.isRunning()
        generating = self._generate_worker is not None and self._generate_worker.isRunning()
        source = self.source_combo.currentData()
        self.btn_research.setEnabled(not researching)
        self.btn_generate.setEnabled(not researching and not generating)
        self.btn_generate.setText("Cover einpassen" if source == SOURCE_BLUR else "Motive erzeugen")
        self.count_spin.setEnabled(source == SOURCE_AI)
        self.style_combo.setEnabled(source == SOURCE_AI)
        self.btn_save.setEnabled(self.current_art is not None and not generating)
        self.btn_delete.setEnabled(card_store.has_card(self.folder.path))

    def _save(self):
        if self.current_art is None:
            return
        QSettings().setValue(ai_settings.SETTINGS_ORIENTATION, self.orientation_combo.currentData())
        QSettings().setValue(ai_settings.SETTINGS_STYLE, self.style_combo.currentText())
        try:
            card_store.save_card(self.folder.path, self.current_art, self._meta(), self.folder.index)
        except OSError as e:
            QMessageBox.warning(self, "Speichern fehlgeschlagen", str(e))
            return
        self.folder.card_path = str(card_store.card_paths(self.folder.path)[0])
        self.saved = True
        self.accept()

    def _delete(self):
        card_store.delete_card(self.folder.path)
        self.folder.card_path = ""
        self.saved = True
        self._update_buttons()
        self.status_label.setText("Kartencover gelöscht.")

    def done(self, result):
        for worker in (self._research_worker, self._generate_worker):
            if worker is not None and worker.isRunning():
                if hasattr(worker, "cancel"):
                    worker.cancel()
                worker.wait(500)
        super().done(result)


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
