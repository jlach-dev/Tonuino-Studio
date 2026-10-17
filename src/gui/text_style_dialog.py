"""
Popup fuer Schriftart, Groesse und Farbe der Kartentexte (Reihe und Titel)

Nicht-modal: jede Aenderung wird sofort gemeldet (styles_changed), damit die
Kartenvorschau live mitgeht. "Abbrechen" stellt die Ausgangswerte wieder her.
"""

from PyQt6.QtCore import pyqtSignal
from PyQt6.QtGui import QColor, QFont
from PyQt6.QtWidgets import (
    QCheckBox, QColorDialog, QDialog, QDialogButtonBox, QDoubleSpinBox, QFontComboBox, QFormLayout, QGroupBox,
    QHBoxLayout, QPushButton, QVBoxLayout,
)

from core.card_layout import TextStyle


class TextStyleEditor(QGroupBox):
    """Ein Bereich (Reihe oder Titel): Schriftart, Groesse, Fett, Farbe"""
    changed = pyqtSignal()

    def __init__(self, title: str, style: TextStyle, parent=None):
        super().__init__(title, parent)
        form = QFormLayout(self)

        self.use_default = QCheckBox("Standardschrift")
        self.family_combo = QFontComboBox()
        family_row = QHBoxLayout()
        family_row.addWidget(self.family_combo, 1)
        family_row.addWidget(self.use_default)
        form.addRow("Schriftart:", family_row)

        self.size_spin = QDoubleSpinBox()
        self.size_spin.setRange(0.0, 15.0)
        self.size_spin.setSingleStep(0.5)
        self.size_spin.setDecimals(1)
        self.size_spin.setSpecialValueText("automatisch")
        self.size_spin.setSuffix(" mm")
        form.addRow("Größe:", self.size_spin)

        self.bold_check = QCheckBox("Fett")
        self.color_button = QPushButton()
        self.color_button.setFixedWidth(110)
        self.color_button.clicked.connect(self._pick_color)
        extras = QHBoxLayout()
        extras.addWidget(self.bold_check)
        extras.addStretch(1)
        extras.addWidget(self.color_button)
        form.addRow("Farbe:", extras)

        self.set_style(style)
        self.use_default.toggled.connect(self._on_default_toggled)
        self.family_combo.currentFontChanged.connect(lambda _f: self.changed.emit())
        self.size_spin.valueChanged.connect(lambda _v: self.changed.emit())
        self.bold_check.toggled.connect(lambda _b: self.changed.emit())

    def set_style(self, style: TextStyle):
        self.blockSignals(True)
        for widget in (self.use_default, self.family_combo, self.size_spin, self.bold_check):
            widget.blockSignals(True)
        if style.family:
            self.family_combo.setCurrentFont(QFont(style.family))
        self.use_default.setChecked(not style.family)
        self.family_combo.setEnabled(bool(style.family))
        self.size_spin.setValue(style.size_mm)
        self.bold_check.setChecked(style.bold)
        self._set_color(style.color)
        for widget in (self.use_default, self.family_combo, self.size_spin, self.bold_check):
            widget.blockSignals(False)
        self.blockSignals(False)

    def _on_default_toggled(self, on: bool):
        self.family_combo.setEnabled(not on)
        self.changed.emit()

    def _set_color(self, color: str):
        self._color = color
        self.color_button.setText(color)
        text = "#000000" if QColor(color).lightness() > 140 else "#ffffff"
        self.color_button.setStyleSheet(f"background-color: {color}; color: {text};")

    def _pick_color(self):
        color = QColorDialog.getColor(QColor(self._color), self, "Textfarbe")
        if color.isValid():
            self._set_color(color.name())
            self.changed.emit()

    def style(self) -> TextStyle:
        return TextStyle(
            family="" if self.use_default.isChecked() else self.family_combo.currentFont().family(),
            size_mm=self.size_spin.value(),
            bold=self.bold_check.isChecked(),
            color=self._color,
        )


class TextStylesPopup(QDialog):
    """Beide Bereiche in einem Popup; Aenderungen sofort per styles_changed"""
    styles_changed = pyqtSignal(object, object)  # series TextStyle, title TextStyle

    def __init__(self, series_style: TextStyle, title_style: TextStyle, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Schrift der Kartentexte")
        self.setMinimumWidth(420)
        self._original = (TextStyle.from_dict(series_style.to_dict()), TextStyle.from_dict(title_style.to_dict()))

        layout = QVBoxLayout(self)
        self.series_editor = TextStyleEditor("Reihe (klein)", series_style)
        self.title_editor = TextStyleEditor("Titel", title_style)
        layout.addWidget(self.series_editor)
        layout.addSpacing(8)
        layout.addWidget(self.title_editor)
        for editor in (self.series_editor, self.title_editor):
            editor.changed.connect(self._emit)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Fertig")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("Abbrechen")
        reset = buttons.addButton("Zurücksetzen", QDialogButtonBox.ButtonRole.ResetRole)
        reset.clicked.connect(self.reset)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _emit(self):
        self.styles_changed.emit(self.series_editor.style(), self.title_editor.style())

    def reset(self):
        self.series_editor.set_style(TextStyle())
        self.title_editor.set_style(TextStyle())
        self._emit()

    def styles(self):
        return self.series_editor.style(), self.title_editor.style()

    def reject(self):
        # Ausgangswerte wiederherstellen (Vorschau folgt)
        self.styles_changed.emit(*self._original)
        super().reject()
