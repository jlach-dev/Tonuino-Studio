"""
Dialog: Kartencover als A4-PDF fuer Etikettenboegen ausgeben
"""

from typing import List

from PIL import Image
from PyQt6.QtCore import Qt, QSettings
from PyQt6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QDoubleSpinBox, QFileDialog, QFormLayout, QHBoxLayout, QLabel,
    QListWidget, QListWidgetItem, QMessageBox, QPushButton, QSpinBox, QVBoxLayout,
)

from core import ai_settings, card_store
from core.sd_card import Folder
from core.sheet_pdf import DEFAULT_TEMPLATE, LABEL_TEMPLATES, SheetItem, build_sheets, save_pdf


class PrintSheetDialog(QDialog):
    def __init__(self, folders: List[Folder], folder_names: dict, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Druckbogen für Kartencover")
        self.setMinimumSize(480, 480)
        self.setSizeGripEnabled(True)
        self.folders = [f for f in folders if f.card_path]
        settings = QSettings()

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("Karten auswählen (nur Ordner mit gespeichertem Kartencover):"))

        self.folder_list = QListWidget()
        for folder in self.folders:
            name = folder_names.get(folder.index) or f"Ordner {folder.index:02d}"
            item = QListWidgetItem(f"{folder.index:02d} – {name}")
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(Qt.CheckState.Checked)
            item.setData(Qt.ItemDataRole.UserRole, folder)
            self.folder_list.addItem(item)
        layout.addWidget(self.folder_list, 1)

        toggle_row = QHBoxLayout()
        for text, state in (("Alle", Qt.CheckState.Checked), ("Keine", Qt.CheckState.Unchecked)):
            button = QPushButton(text)
            button.setObjectName("ghostButton")
            button.clicked.connect(lambda _=False, s=state: self._check_all(s))
            toggle_row.addWidget(button)
        toggle_row.addStretch(1)
        layout.addLayout(toggle_row)

        form = QFormLayout()
        self.template_combo = QComboBox()
        for template in LABEL_TEMPLATES.values():
            self.template_combo.addItem(template.name, template.key)
        index = self.template_combo.findData(settings.value(ai_settings.SETTINGS_TEMPLATE, DEFAULT_TEMPLATE, type=str))
        self.template_combo.setCurrentIndex(max(0, index))
        self.template_combo.currentIndexChanged.connect(self._on_template_changed)
        form.addRow("Etikettenbogen:", self.template_combo)

        self.template_note = QLabel()
        self.template_note.setWordWrap(True)
        form.addRow("", self.template_note)

        self.copies_spin = QSpinBox()
        self.copies_spin.setRange(1, 10)
        form.addRow("Exemplare je Karte:", self.copies_spin)

        self.backs_check = QCheckBox("Rückseiten mitdrucken (als weitere Etiketten nach den Vorderseiten)")
        self.backs_check.setChecked(any(card_store.has_back(f.path) for f in self.folders))
        self.backs_check.setEnabled(self.backs_check.isChecked())
        form.addRow("", self.backs_check)

        self.start_spin = QSpinBox()
        self.start_spin.setToolTip("Für angebrochene Bögen: erstes freies Etikett (zeilenweise von links oben)")
        form.addRow("Beginnen bei Etikett Nr.:", self.start_spin)

        self.offset_x = self._offset_spin(settings.value(ai_settings.SETTINGS_OFFSET_X, 0.0, type=float))
        self.offset_y = self._offset_spin(settings.value(ai_settings.SETTINGS_OFFSET_Y, 0.0, type=float))
        offset_row = QHBoxLayout()
        offset_row.addWidget(QLabel("X"))
        offset_row.addWidget(self.offset_x)
        offset_row.addWidget(QLabel("Y"))
        offset_row.addWidget(self.offset_y)
        offset_row.addStretch(1)
        form.addRow("Druck-Versatz (mm):", offset_row)
        layout.addLayout(form)

        hint = QLabel("Beim Drucken „Tatsächliche Größe / 100 %“ wählen, nicht „An Seite anpassen“.")
        hint.setWordWrap(True)
        layout.addWidget(hint)

        buttons = QHBoxLayout()
        btn_test = QPushButton("Testseite speichern …")
        btn_test.setObjectName("ghostButton")
        btn_test.clicked.connect(lambda: self._export(test_page=True))
        buttons.addWidget(btn_test)
        buttons.addStretch(1)
        btn_close = QPushButton("Schließen")
        btn_close.clicked.connect(self.reject)
        buttons.addWidget(btn_close)
        btn_pdf = QPushButton("PDF speichern …")
        btn_pdf.setObjectName("primaryButton")
        btn_pdf.clicked.connect(lambda: self._export(test_page=False))
        buttons.addWidget(btn_pdf)
        layout.addLayout(buttons)

        self._on_template_changed()

    @staticmethod
    def _offset_spin(value: float) -> QDoubleSpinBox:
        spin = QDoubleSpinBox()
        spin.setRange(-10.0, 10.0)
        spin.setSingleStep(0.5)
        spin.setDecimals(1)
        spin.setValue(value)
        return spin

    def _template(self):
        return LABEL_TEMPLATES[self.template_combo.currentData()]

    def _on_template_changed(self, *_):
        template = self._template()
        self.start_spin.setRange(1, template.per_sheet)
        self.template_note.setText(template.note)

    def _check_all(self, state):
        for row in range(self.folder_list.count()):
            self.folder_list.item(row).setCheckState(state)

    def _items(self) -> List[SheetItem]:
        """Rendert jede gewaehlte Karte passend zur Etikettengroesse neu aus dem
        Rohmotiv (card_art_raw.png) - faellt sonst auf card.png zurueck."""
        spec = self._template().card_spec()
        items = []
        backs = []
        for row in range(self.folder_list.count()):
            item = self.folder_list.item(row)
            if item.checkState() != Qt.CheckState.Checked:
                continue
            folder: Folder = item.data(Qt.ItemDataRole.UserRole)
            meta = card_store.load_meta(folder.path) or card_store.CardMeta()
            art = card_store.load_raw_art(folder.path)
            if art is not None:
                image = card_store.render_from_meta(art, meta, folder.index, spec)
            else:
                with Image.open(folder.card_path) as img:
                    image = img.convert("RGB")
            items.extend([SheetItem(image, meta.orientation)] * self.copies_spin.value())
            if self.backs_check.isChecked():
                back_raw = card_store.load_back_raw(folder.path)
                if back_raw is not None:
                    back = card_store.render_back_from_meta(back_raw, meta, spec)
                    backs.extend([SheetItem(back, meta.orientation)] * self.copies_spin.value())
        return items + backs

    def _export(self, test_page: bool):
        template = self._template()
        settings = QSettings()
        settings.setValue(ai_settings.SETTINGS_TEMPLATE, template.key)
        settings.setValue(ai_settings.SETTINGS_OFFSET_X, self.offset_x.value())
        settings.setValue(ai_settings.SETTINGS_OFFSET_Y, self.offset_y.value())

        items = [] if test_page else self._items()
        if not test_page and not items:
            QMessageBox.information(self, "Keine Karten", "Bitte mindestens eine Karte auswählen.")
            return

        default_name = "Testseite.pdf" if test_page else "Kartencover.pdf"
        path, _ = QFileDialog.getSaveFileName(self, "PDF speichern", default_name, "PDF (*.pdf)")
        if not path:
            return
        try:
            pages = build_sheets(items, template, start_slot=self.start_spin.value() - 1,
                                 offset_mm=(self.offset_x.value(), self.offset_y.value()), outline=test_page)
            save_pdf(pages, path)
        except Exception as e:
            QMessageBox.warning(self, "PDF fehlgeschlagen", str(e))
            return
        QMessageBox.information(self, "PDF gespeichert", f"{len(pages)} Seite(n) gespeichert:\n{path}")
