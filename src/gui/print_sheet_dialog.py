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
from core.sheet_pdf import (
    ALIGN_CENTER, ALIGN_END, ALIGN_START, CARD_ISO_MM, DEFAULT_TEMPLATE, LABEL_TEMPLATES, Placement, SheetItem,
    build_sheets, layout_for, save_pdf,
)

FORMAT_ISO, FORMAT_CUSTOM, FORMAT_FILL = "iso", "custom", "fill"
FORMATS = (
    (FORMAT_ISO, "Standard-RFID-Karte (85,6 × 53,98 mm)"),
    (FORMAT_CUSTOM, "Eigenes Kartenformat"),
    (FORMAT_FILL, "Etikett ganz füllen"),
)
ALIGN_X = ((ALIGN_START, "links"), (ALIGN_CENTER, "mittig"), (ALIGN_END, "rechts"))
ALIGN_Y = ((ALIGN_START, "oben"), (ALIGN_CENTER, "mittig"), (ALIGN_END, "unten"))


def _card_dir(folder: Folder) -> str:
    """Ordner mit card.png/card.json: auf der SD-Karte der Ordner selbst, bei
    Mediahub-Karten (core/mediahub_cards.py) der Kartenordner im Archiv"""
    return getattr(folder, "card_dir", "") or folder.path


def _label(folder: Folder, folder_names: dict) -> str:
    if folder.index is None:
        return folder.name or "Karte"
    name = folder_names.get(folder.index) or f"Ordner {folder.index:02d}"
    return f"{folder.index:02d} – {name}"


def _mm(value: float) -> str:
    return f"{value:g}".replace(".", ",")


class PrintSheetDialog(QDialog):
    def __init__(self, folders: List[Folder], folder_names: dict, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Druckbogen für Kartencover")
        self.setMinimumSize(480, 480)
        self.setSizeGripEnabled(True)
        self.folders = [f for f in folders if f.card_path]
        settings = QSettings()

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("Karten auswählen (nur Karten mit gespeichertem Kartencover):"))

        self.folder_list = QListWidget()
        for folder in self.folders:
            item = QListWidgetItem(_label(folder, folder_names))
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

        self.format_combo = QComboBox()
        for key, text in FORMATS:
            self.format_combo.addItem(text, key)
        self.format_combo.setToolTip("Wie groß das Cover gedruckt wird. Standard: so groß wie die RFID-Karte, "
                                     "auch wenn das Etikett größer ist")
        self.format_combo.setCurrentIndex(max(0, self.format_combo.findData(
            settings.value(ai_settings.SETTINGS_CARD_FORMAT, FORMAT_ISO, type=str))))
        form.addRow("Kartenformat:", self.format_combo)
        self.card_w = self._mm_spin(settings.value(ai_settings.SETTINGS_CARD_W, CARD_ISO_MM[0], type=float))
        self.card_h = self._mm_spin(settings.value(ai_settings.SETTINGS_CARD_H, CARD_ISO_MM[1], type=float))
        size_row = QHBoxLayout()
        size_row.addWidget(self.card_w)
        size_row.addWidget(QLabel("×"))
        size_row.addWidget(self.card_h)
        size_row.addWidget(QLabel("mm"))
        size_row.addStretch(1)
        form.addRow("", size_row)

        self.align_x = QComboBox()
        for key, text in ALIGN_X:
            self.align_x.addItem(text, key)
        self.align_y = QComboBox()
        for key, text in ALIGN_Y:
            self.align_y.addItem(text, key)
        self.align_x.setCurrentIndex(max(0, self.align_x.findData(
            settings.value(ai_settings.SETTINGS_ALIGN_X, ALIGN_START, type=str))))
        self.align_y.setCurrentIndex(max(0, self.align_y.findData(
            settings.value(ai_settings.SETTINGS_ALIGN_Y, ALIGN_START, type=str))))
        align_row = QHBoxLayout()
        align_row.addWidget(self.align_y)
        align_row.addWidget(self.align_x)
        align_row.addStretch(1)
        self.align_label = QLabel("Lage im Etikett:")
        form.addRow(self.align_label, align_row)
        self.placement_note = QLabel()
        self.placement_note.setWordWrap(True)
        form.addRow("", self.placement_note)
        for combo in (self.format_combo, self.align_x, self.align_y):
            combo.currentIndexChanged.connect(self._update_placement)
        for spin in (self.card_w, self.card_h):
            spin.valueChanged.connect(self._update_placement)

        self.copies_spin = QSpinBox()
        self.copies_spin.setRange(1, 10)
        form.addRow("Exemplare je Karte:", self.copies_spin)

        self.backs_check = QCheckBox("Rückseiten mitdrucken")
        self.backs_check.setToolTip("Als weitere Etiketten nach den Vorderseiten")
        self.backs_check.setChecked(any(card_store.has_back(_card_dir(f)) for f in self.folders))
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

    @staticmethod
    def _mm_spin(value: float) -> QDoubleSpinBox:
        spin = QDoubleSpinBox()
        spin.setRange(20.0, 200.0)
        spin.setDecimals(2)
        spin.setSingleStep(0.5)
        spin.setValue(value)
        return spin

    def _template(self):
        """Bogen samt Raster fuer das gewaehlte Kartenformat (Schnittmarken-Boegen)"""
        return layout_for(LABEL_TEMPLATES[self.template_combo.currentData()], self._placement())

    def _placement(self) -> Placement:
        fmt = self.format_combo.currentData()
        w, h = (self.card_w.value(), self.card_h.value()) if fmt == FORMAT_CUSTOM else CARD_ISO_MM
        return Placement(card_w=max(w, h), card_h=min(w, h), fill_label=fmt == FORMAT_FILL,
                         align_x=self.align_x.currentData(), align_y=self.align_y.currentData())

    def _on_template_changed(self, *_):
        self.template_note.setText(LABEL_TEMPLATES[self.template_combo.currentData()].note)
        self._update_placement()

    def _update_placement(self, *_):
        if not hasattr(self, "start_spin"):  # noch im Aufbau
            return
        fmt = self.format_combo.currentData()
        self.card_w.setEnabled(fmt == FORMAT_CUSTOM)
        self.card_h.setEnabled(fmt == FORMAT_CUSTOM)
        template = self._template()
        placement = self._placement()
        w, h = placement.size_mm(template)
        if fmt == FORMAT_ISO:  # Felder zeigen das Standardformat
            for spin, value in zip((self.card_w, self.card_h), CARD_ISO_MM):
                spin.blockSignals(True)
                spin.setValue(value)
                spin.blockSignals(False)
        fixed = placement.fill_label or template.cut_marks
        for widget in (self.align_label, self.align_x, self.align_y):
            widget.setEnabled(not fixed)
        label = f"{_mm(template.label_w)} × {_mm(template.label_h)} mm"
        if template.cut_marks:
            note = f"Zum Ausschneiden: {template.per_sheet} Karten je Bogen in Kartengröße."
        elif placement.fill_label:
            note = f"Das Cover füllt das ganze Etikett ({label})."
        elif w > template.label_w + 0.01 or h > template.label_h + 0.01:
            note = (f"Die Karte ist größer als das Etikett ({label}): Das Cover wird am Etikettenrand "
                    "abgeschnitten. Für Aufkleber auf die Karte besser „Etikett ganz füllen“ wählen.")
        else:
            note = (f"Etikett {label}, Karte {_mm(w)} × {_mm(h)} mm. Liegt die Karte an zwei Kanten des "
                    "Etiketts, schneidest du nur noch an zwei Seiten.")
        self.placement_note.setText(note)
        self.start_spin.setRange(1, template.per_sheet)

    def _check_all(self, state):
        for row in range(self.folder_list.count()):
            self.folder_list.item(row).setCheckState(state)

    def _items(self) -> List[SheetItem]:
        """Rendert jede gewaehlte Karte passend zur Etikettengroesse neu aus dem
        Rohmotiv (card_art_raw.png) - faellt sonst auf card.png zurueck."""
        spec = self._placement().card_spec(self._template())
        items = []
        backs = []
        for row in range(self.folder_list.count()):
            item = self.folder_list.item(row)
            if item.checkState() != Qt.CheckState.Checked:
                continue
            folder: Folder = item.data(Qt.ItemDataRole.UserRole)
            card_dir = _card_dir(folder)
            meta = card_store.load_meta(card_dir) or card_store.CardMeta()
            art = card_store.load_raw_art(card_dir)
            if art is not None:
                image = card_store.render_from_meta(art, meta, folder.index, spec)
            else:
                with Image.open(folder.card_path) as img:
                    image = img.convert("RGB")
            items.extend([SheetItem(image, meta.orientation)] * self.copies_spin.value())
            if self.backs_check.isChecked():
                back_raw = card_store.load_back_raw(card_dir)
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
        placement = self._placement()
        settings.setValue(ai_settings.SETTINGS_CARD_FORMAT, self.format_combo.currentData())
        if self.format_combo.currentData() == FORMAT_CUSTOM:
            settings.setValue(ai_settings.SETTINGS_CARD_W, self.card_w.value())
            settings.setValue(ai_settings.SETTINGS_CARD_H, self.card_h.value())
        settings.setValue(ai_settings.SETTINGS_ALIGN_X, placement.align_x)
        settings.setValue(ai_settings.SETTINGS_ALIGN_Y, placement.align_y)

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
                                 offset_mm=(self.offset_x.value(), self.offset_y.value()), outline=test_page,
                                 placement=placement)
            save_pdf(pages, path)
        except Exception as e:
            QMessageBox.warning(self, "PDF fehlgeschlagen", str(e))
            return
        QMessageBox.information(self, "PDF gespeichert", f"{len(pages)} Seite(n) gespeichert:\n{path}")
