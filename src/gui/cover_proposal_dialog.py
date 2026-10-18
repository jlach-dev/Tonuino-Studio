"""
Pruefdialog der Cover-Stapelsuche: eine Zeile je Album mit Vorschaubild,
Quelle und Bewertung; sichere Treffer sind vorangehakt. Erst "Uebernehmen"
schreibt die angehakten Cover (Dateien, cover.jpg, Archiv).
"""

from typing import List, Optional

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QPixmap
from PyQt6.QtWidgets import (
    QAbstractItemView, QApplication, QCheckBox, QDialog, QHBoxLayout, QHeaderView, QLabel, QMenu, QMessageBox,
    QProgressDialog, QPushButton, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

from core import cover_batch, cover_sources
from core.cover_batch import AlbumProposal
from gui.common import confirm_action

THUMB = 72
COL_CHECK, COL_THUMB, COL_ALBUM, COL_HIT, COL_NOTE = range(5)


def _pixmap(data: Optional[bytes]) -> Optional[QPixmap]:
    if not data:
        return None
    pixmap = QPixmap()
    if not pixmap.loadFromData(data):
        return None
    return pixmap.scaled(THUMB, THUMB, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)


class CoverProposalDialog(QDialog):
    def __init__(self, proposals: List[AlbumProposal], archive, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Cover nachtragen – Vorschläge prüfen")
        self.setWindowFlags(self.windowFlags() | Qt.WindowType.WindowMaximizeButtonHint)
        self.resize(1100, 640)
        self.proposals = list(proposals)
        self.archive = archive
        self.result = None  # EmbedResult nach dem Uebernehmen
        self.open_requested = None  # LibraryAlbum, das im Cover-Manager geoeffnet werden soll
        self.checks: List[QCheckBox] = []
        layout = QVBoxLayout(self)

        intro = QLabel("Je Album der beste Treffer: sichere Treffer (klarer Spitzenreiter nach Titel, Interpret und "
                       "Trackanzahl) sind angehakt, unsichere nicht. Rechtsklick auf einen Vorschlag zeigt weitere "
                       "Treffer. Geschrieben wird erst mit „Übernehmen“: in die Dateien, als cover.jpg und ins Archiv.")
        intro.setWordWrap(True)
        layout.addWidget(intro)

        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(["", "Cover", "Album", "Vorschlag", "Bewertung"])
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(COL_CHECK, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(COL_THUMB, QHeaderView.ResizeMode.Fixed)
        header.resizeSection(COL_THUMB, THUMB + 12)
        header.setSectionResizeMode(COL_ALBUM, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(COL_HIT, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(COL_NOTE, QHeaderView.ResizeMode.Stretch)
        self.table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._context_menu)
        layout.addWidget(self.table, 1)
        self._fill()

        self.summary = QLabel()
        self.summary.setWordWrap(True)
        layout.addWidget(self.summary)

        buttons = QHBoxLayout()
        self.btn_open = QPushButton("Im Cover-Manager öffnen …")
        self.btn_open.setObjectName("ghostButton")
        self.btn_open.setToolTip("Das markierte Album im Cover-Manager öffnen und das Cover selbst wählen")
        self.btn_open.clicked.connect(self._open_in_manager)
        buttons.addWidget(self.btn_open)
        btn_all = QPushButton("Alle")
        btn_all.setObjectName("ghostButton")
        btn_all.clicked.connect(lambda: self._check_all(True))
        buttons.addWidget(btn_all)
        btn_none = QPushButton("Keine")
        btn_none.setObjectName("ghostButton")
        btn_none.clicked.connect(lambda: self._check_all(False))
        buttons.addWidget(btn_none)
        buttons.addStretch(1)
        btn_cancel = QPushButton("Abbrechen")
        btn_cancel.clicked.connect(self.reject)
        buttons.addWidget(btn_cancel)
        self.btn_apply = QPushButton("Übernehmen")
        self.btn_apply.setObjectName("primaryButton")
        self.btn_apply.clicked.connect(self._apply)
        buttons.addWidget(self.btn_apply)
        layout.addLayout(buttons)
        self._update_summary()

    # --- Tabelle --------------------------------------------------------------------------

    def _fill(self):
        self.table.setRowCount(len(self.proposals))
        self.checks = []
        for row, proposal in enumerate(self.proposals):
            check = QCheckBox()
            check.setChecked(proposal.certain and proposal.usable)
            check.setEnabled(proposal.usable)
            check.toggled.connect(self._update_summary)
            holder = QWidget()
            holder_layout = QHBoxLayout(holder)
            holder_layout.setContentsMargins(6, 0, 6, 0)
            holder_layout.addWidget(check, 0, Qt.AlignmentFlag.AlignCenter)
            self.table.setCellWidget(row, COL_CHECK, holder)
            self.checks.append(check)
            self.table.setItem(row, COL_ALBUM, QTableWidgetItem(
                proposal.album.name + (f"\n{proposal.album.artist}" if proposal.album.artist else "")
                + f"\n{proposal.album.track_count} Tracks"))
            self._fill_hit(row, proposal)
            self.table.setRowHeight(row, THUMB + 10)

    def _fill_hit(self, row: int, proposal: AlbumProposal):
        thumb = QLabel()
        thumb.setAlignment(Qt.AlignmentFlag.AlignCenter)
        pixmap = _pixmap(proposal.data)
        if pixmap is not None:
            thumb.setPixmap(pixmap)
        else:
            thumb.setText("–")
        self.table.setCellWidget(row, COL_THUMB, thumb)
        hit = proposal.hit
        self.table.setItem(row, COL_HIT, QTableWidgetItem(hit.label if hit else "kein Vorschlag"))
        score = f"{int(hit.score)} Punkte · " if hit and hit.source not in (cover_batch.SOURCE_ARCHIVE,) else ""
        note = QTableWidgetItem(score + proposal.note)
        if proposal.usable and not proposal.certain:
            note.setForeground(Qt.GlobalColor.yellow)
        self.table.setItem(row, COL_NOTE, note)

    def _check_all(self, state: bool):
        for check in self.checks:
            if check.isEnabled():
                check.setChecked(state)

    def _update_summary(self):
        certain = sum(1 for p in self.proposals if p.usable and p.certain)
        unsure = sum(1 for p in self.proposals if p.usable and not p.certain)
        none = len(self.proposals) - certain - unsure
        checked = sum(1 for c in self.checks if c.isChecked())
        self.summary.setText(f"{certain} sicher, {unsure} bitte prüfen, {none} ohne Treffer · "
                             f"{checked} angehakt")
        self.btn_apply.setEnabled(checked > 0)

    def _current_row(self) -> int:
        return self.table.currentRow()

    # --- Aktionen -------------------------------------------------------------------------

    def _context_menu(self, pos):
        row = self.table.rowAt(pos.y())
        if row < 0:
            return
        proposal = self.proposals[row]
        if not proposal.alternatives:
            return
        menu = QMenu(self)
        for alternative in proposal.alternatives:
            action = menu.addAction(f"{alternative.label} ({int(alternative.score)} Punkte)")
            action.triggered.connect(lambda _=False, r=row, a=alternative: self._use_alternative(r, a))
        menu.exec(self.table.viewport().mapToGlobal(pos))

    def _use_alternative(self, row: int, hit, download=None):
        """Anderen Treffer waehlen: Bild laden und die Zeile ersetzen"""
        download = download or cover_sources.download_image
        proposal = self.proposals[row]
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            data = download(hit.image_url)
        finally:
            QApplication.restoreOverrideCursor()
        if not data:
            QMessageBox.warning(self, "Cover nachtragen", "Dieses Bild ließ sich nicht laden.")
            return
        previous = proposal.hit
        proposal.alternatives = [h for h in ([previous] if previous else []) + proposal.alternatives if h is not hit]
        proposal.hit, proposal.data, proposal.certain = hit, data, False
        proposal.note = "von Hand gewählt · " + ", ".join(hit.reasons)
        self._fill_hit(row, proposal)
        self.checks[row].setEnabled(True)
        self.checks[row].setChecked(True)
        self._update_summary()

    def _open_in_manager(self):
        row = self._current_row()
        if row < 0:
            QMessageBox.information(self, "Cover nachtragen", "Bitte zuerst ein Album in der Liste markieren.")
            return
        self.open_requested = self.proposals[row].album
        self.reject()

    def selected_proposals(self) -> List[AlbumProposal]:
        return [p for p, c in zip(self.proposals, self.checks) if c.isChecked() and p.usable]

    def _apply(self):
        chosen = self.selected_proposals()
        if not chosen:
            return
        if not confirm_action(self, "Cover nachtragen", f"{len(chosen)} Cover werden in die Dateien geschrieben, "
                                                         "als cover.jpg abgelegt und im Archiv als Album-Cover "
                                                         "gesetzt.\n\nFortfahren?", default_no=False):
            return
        progress = QProgressDialog("Cover werden geschrieben …", None, 0, len(chosen), self)
        progress.setWindowTitle("Cover nachtragen")
        progress.setWindowModality(Qt.WindowModality.WindowModal)
        progress.setMinimumDuration(0)
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            self.result = cover_batch.apply_proposals(
                chosen, self.archive,
                progress=lambda cur, total, name: (progress.setLabelText(f"({cur}/{total}) {name}"),
                                                   progress.setValue(cur), QApplication.processEvents()))
        finally:
            QApplication.restoreOverrideCursor()
            progress.close()
        self.accept()
