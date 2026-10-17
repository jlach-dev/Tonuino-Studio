"""
Ergebnis der Stueck-Erkennung (AcoustID) fuer Dateien ohne gemeinsames Album:
je Datei der beste Treffer, auswaehlbar, Schreiben von Titel/Interpret/Album.
"""

from typing import List

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QAbstractItemView, QDialog, QHBoxLayout, QHeaderView, QLabel, QPushButton, QTableWidget, QTableWidgetItem,
    QVBoxLayout,
)

from core.acoustid import RecognitionResult
from core.library import write_tags_any
from gui.common import confirm_action

MB_RECORDING_ID_DESC = "MusicBrainz Recording Id"


class RecognitionDialog(QDialog):
    def __init__(self, result: RecognitionResult, parent=None, label: str = ""):
        super().__init__(parent)
        self.setWindowTitle(f"Stücke erkannt – {label}" if label else "Stücke erkannt")
        self.resize(900, 520)
        self.setWindowFlags(self.windowFlags() | Qt.WindowType.WindowMaximizeButtonHint)
        self.result = result
        self.written = 0
        layout = QVBoxLayout(self)

        recognized = result.recognized
        intro = QLabel(f"{recognized} von {len(result.files)} Datei(en) erkannt. Die Dateien gehören zu keinem "
                       "gemeinsamen Album – je Datei wird der beste Treffer übernommen (Titel, Interpret, Album).")
        intro.setWordWrap(True)
        layout.addWidget(intro)

        self.table = QTableWidget(len(result.files), 4)
        self.table.setHorizontalHeaderLabels(["Übernehmen", "Datei", "Erkannt", "Sicherheit"])
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        import os
        for row, item in enumerate(result.files):
            check = QTableWidgetItem()
            check.setFlags(Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsEnabled)
            best = item.best
            check.setCheckState(Qt.CheckState.Checked if best else Qt.CheckState.Unchecked)
            self.table.setItem(row, 0, check)
            self.table.setItem(row, 1, QTableWidgetItem(os.path.basename(item.path)))
            if best:
                text = f"{best.title} – {best.artist}" + (f" ({best.release_group_title})" if best.release_group_title else "")
                score = f"{best.score:.0%}"
            else:
                text, score = item.error or "nicht erkannt", ""
            self.table.setItem(row, 2, QTableWidgetItem(text))
            self.table.setItem(row, 3, QTableWidgetItem(score))
        layout.addWidget(self.table, 1)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        btn_cancel = QPushButton("Abbrechen")
        btn_cancel.clicked.connect(self.reject)
        buttons.addWidget(btn_cancel)
        self.btn_apply = QPushButton("Tags übernehmen")
        self.btn_apply.setObjectName("primaryButton")
        self.btn_apply.setEnabled(recognized > 0)
        self.btn_apply.clicked.connect(self._apply)
        buttons.addWidget(self.btn_apply)
        layout.addLayout(buttons)

    def selected_rows(self) -> List[int]:
        return [row for row in range(self.table.rowCount())
                if self.table.item(row, 0).checkState() == Qt.CheckState.Checked and self.result.files[row].best]

    def _apply(self):
        rows = self.selected_rows()
        if not rows:
            return
        if not confirm_action(self, "Tags übernehmen", f"Titel, Interpret und Album von {len(rows)} Datei(en) "
                                                       "werden in die Audiodateien geschrieben. Fortfahren?",
                              default_no=False):
            return
        self.written = apply_recognition(self.result, rows)
        self.accept()


def apply_recognition(result: RecognitionResult, rows: List[int]) -> int:
    written = 0
    for row in rows:
        item = result.files[row]
        best = item.best
        if best is None:
            continue
        fields = {"title": best.title, "artist": best.artist}
        if best.release_group_title:
            fields["album"] = best.release_group_title
            if best.release_group_artist:
                fields["album_artist"] = best.release_group_artist
        custom = {MB_RECORDING_ID_DESC: best.recording_id} if best.recording_id else {}
        if write_tags_any(item.path, fields, custom):
            written += 1
    return written
