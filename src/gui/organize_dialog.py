"""
Dialog "Dateien einsortieren": Vorschau alt -> neu nach Schema, dann Verschieben
"""

import os
from typing import List

from PyQt6.QtCore import QSettings, Qt
from PyQt6.QtWidgets import (
    QAbstractItemView, QApplication, QDialog, QFormLayout, QHBoxLayout, QHeaderView, QLabel, QLineEdit,
    QMessageBox, QPushButton, QTableWidget, QTableWidgetItem, QVBoxLayout,
)

from core.library_organize import DEFAULT_SCHEME, MovePlan, execute_moves, plan_moves
from gui.common import confirm_action

SETTINGS_SCHEME = "library/organize_scheme"
STATUS_TEXT = {"ok": "wird verschoben", "unchanged": "liegt schon richtig", "missing_tags": "Tags fehlen",
               "conflict": "Konflikt", "too_long": "Pfad zu lang"}


class OrganizeDialog(QDialog):
    def __init__(self, paths: List[str], root: str, parent=None, label: str = ""):
        super().__init__(parent)
        self.setWindowTitle(f"Dateien einsortieren – {label}" if label else "Dateien einsortieren")
        self.resize(980, 560)
        self.setWindowFlags(self.windowFlags() | Qt.WindowType.WindowMaximizeButtonHint)
        self.paths = list(paths)
        self.root = root
        self.plans: List[MovePlan] = []
        self.result = None
        layout = QVBoxLayout(self)

        intro = QLabel(f"Die Dateien werden nach ihren Tags unterhalb von „{root}“ einsortiert. Vorhandene "
                       "Dateien werden nie überschrieben; leere Ursprungsordner werden entfernt.")
        intro.setWordWrap(True)
        layout.addWidget(intro)

        form = QFormLayout()
        self.scheme_edit = QLineEdit(QSettings().value(SETTINGS_SCHEME, DEFAULT_SCHEME, type=str))
        self.scheme_edit.setToolTip("Platzhalter: {albumartist} {artist} {album} {title} {track:02d} {disc} {year} – "
                                    "bei Alben mit mehreren CDs kommt die CD-Nummer automatisch vor die "
                                    "Tracknummer (1-01, 2-01)")
        self.scheme_edit.editingFinished.connect(self._refresh)
        form.addRow("Schema:", self.scheme_edit)
        layout.addLayout(form)

        self.table = QTableWidget(0, 3)
        self.table.setHorizontalHeaderLabels(["Datei", "Ziel", "Status"])
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        layout.addWidget(self.table, 1)

        self.summary = QLabel()
        self.summary.setWordWrap(True)
        layout.addWidget(self.summary)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        btn_cancel = QPushButton("Abbrechen")
        btn_cancel.clicked.connect(self.reject)
        buttons.addWidget(btn_cancel)
        self.btn_apply = QPushButton("Einsortieren")
        self.btn_apply.setObjectName("primaryButton")
        self.btn_apply.clicked.connect(self._apply)
        buttons.addWidget(self.btn_apply)
        layout.addLayout(buttons)
        self._refresh()

    def _refresh(self):
        scheme = self.scheme_edit.text().strip() or DEFAULT_SCHEME
        QSettings().setValue(SETTINGS_SCHEME, scheme)
        self.plans = plan_moves(self.paths, self.root, scheme)
        self.table.setRowCount(len(self.plans))
        for row, plan in enumerate(self.plans):
            self.table.setItem(row, 0, QTableWidgetItem(os.path.relpath(plan.source, self.root)
                                                        if plan.source.startswith(self.root) else plan.source))
            self.table.setItem(row, 1, QTableWidgetItem(os.path.relpath(plan.target, self.root)
                                                        if plan.target else "–"))
            self.table.setItem(row, 2, QTableWidgetItem(STATUS_TEXT.get(plan.status, plan.status)
                                                        + (f" ({plan.note})" if plan.note and plan.status != "ok" else "")))
        moving = sum(1 for p in self.plans if p.will_move)
        others = len(self.plans) - moving
        self.summary.setText(f"{moving} Datei(en) werden verschoben" + (f", {others} bleiben (siehe Status)." if others else "."))
        self.btn_apply.setEnabled(moving > 0)

    def _apply(self):
        moving = [p for p in self.plans if p.will_move]
        if not moving:
            return
        if not confirm_action(self, "Dateien einsortieren", f"{len(moving)} Datei(en) werden verschoben. "
                                                             "Fortfahren?", default_no=False):
            return
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            self.result = execute_moves(self.plans)
        finally:
            QApplication.restoreOverrideCursor()
        if self.result.errors:
            QMessageBox.warning(self, "Teilweise verschoben", "\n".join(self.result.errors[:10]))
        self.accept()
