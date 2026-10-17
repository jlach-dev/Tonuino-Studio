"""
Popup fuer Schriftart, Groesse und Farbe eines Kartentext-Bereichs (Reihe / Titel)
"""

from PyQt6.QtGui import QColor, QFont
from PyQt6.QtWidgets import (
    QCheckBox, QColorDialog, QDialog, QDialogButtonBox, QDoubleSpinBox, QFontComboBox, QFormLayout, QPushButton,
)

from core.card_layout import TextStyle


class TextStyleDialog(QDialog):
    def __init__(self, title: str, style: TextStyle, parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"Schrift: {title}")
        self.setMinimumWidth(360)
        form = QFormLayout(self)

        self.family_combo = QFontComboBox()
        if style.family:
            self.family_combo.setCurrentFont(QFont(style.family))
        self.use_default = QCheckBox("Standardschrift verwenden")
        self.use_default.setChecked(not style.family)
        self.use_default.toggled.connect(lambda on: self.family_combo.setEnabled(not on))
        self.family_combo.setEnabled(bool(style.family))
        form.addRow("", self.use_default)
        form.addRow("Schriftart:", self.family_combo)

        self.size_spin = QDoubleSpinBox()
        self.size_spin.setRange(0.0, 15.0)
        self.size_spin.setSingleStep(0.5)
        self.size_spin.setDecimals(1)
        self.size_spin.setSpecialValueText("automatisch")
        self.size_spin.setSuffix(" mm")
        self.size_spin.setValue(style.size_mm)
        form.addRow("Größe:", self.size_spin)

        self.bold_check = QCheckBox("Fett")
        self.bold_check.setChecked(style.bold)
        form.addRow("", self.bold_check)

        self.color_button = QPushButton()
        self.color_button.clicked.connect(self._pick_color)
        self._set_color(style.color)
        form.addRow("Farbe:", self.color_button)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Übernehmen")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("Abbrechen")
        reset = buttons.addButton("Zurücksetzen", QDialogButtonBox.ButtonRole.ResetRole)
        reset.clicked.connect(self._reset)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)

    def _set_color(self, color: str):
        self._color = color
        self.color_button.setText(color)
        self.color_button.setStyleSheet(f"background-color: {color}; color: {'#000' if QColor(color).lightness() > 140 else '#fff'};")

    def _pick_color(self):
        color = QColorDialog.getColor(QColor(self._color), self, "Textfarbe")
        if color.isValid():
            self._set_color(color.name())

    def _reset(self):
        self.use_default.setChecked(True)
        self.size_spin.setValue(0.0)
        self.bold_check.setChecked(True)
        self._set_color("#ffffff")

    def result_style(self) -> TextStyle:
        return TextStyle(
            family="" if self.use_default.isChecked() else self.family_combo.currentFont().family(),
            size_mm=self.size_spin.value(),
            bold=self.bold_check.isChecked(),
            color=self._color,
        )
