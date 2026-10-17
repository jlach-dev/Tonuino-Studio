"""
Tags von MusicBrainz: Kandidaten suchen, Diff alt -> neu pruefen, uebernehmen

Oben Suchfelder (Titel/Interpret, Quellen) und die Auswahl des Releases,
darunter die Tabelle mit einer Zeile je Track und einer Spalte je Feld. Jede
geaenderte Zelle hat ein Haekchen; die Haekchen ueber der Tabelle schalten
ganze Spalten, das Haekchen in der Track-Spalte die ganze Zeile. Nicht
zugeordnete Tracks bleiben leer. "Übernehmen" schreibt nur die angehakten
Felder (core/tag_lookup.apply_diff).
"""

import logging
from typing import Dict, List, Optional

from PyQt6.QtCore import Qt, QSettings, QThread, QTimer, pyqtSignal
from PyQt6.QtGui import QColor
from PyQt6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QFormLayout, QHBoxLayout, QHeaderView, QLabel, QLineEdit, QMessageBox,
    QPushButton, QTableWidget, QTableWidgetItem, QVBoxLayout,
)

from core import tag_lookup
from core.sd_card import Folder
from core.tag_lookup import FIELDS, FIELD_MB_IDS, LocalTrack, ReleaseCandidate, TrackDiff
from gui import theme
from gui.common import confirm_action

log = logging.getLogger(__name__)

SETTINGS_SOURCES = "tags/lookup_sources"

COL_TRACK = 0
COL_MATCH = 1
COL_FIELD0 = 2


class TagSearchWorker(QThread):
    """Liest die lokalen Tags (einmal je Datei) und sucht Kandidaten"""
    local_loaded = pyqtSignal(object)  # List[LocalTrack]
    status = pyqtSignal(str)
    completed = pyqtSignal(object)  # List[ReleaseCandidate]
    failed = pyqtSignal(str)

    def __init__(self, folder: Folder, metadata_manager, title: str, artist: str, sources: List[str],
                 local: Optional[List[LocalTrack]] = None):
        super().__init__()
        self.folder = folder
        self.metadata_manager = metadata_manager
        self.title = title
        self.artist = artist
        self.sources = sources
        self.local = local
        self._cancelled = False

    def cancel(self):
        self._cancelled = True

    def run(self):
        try:
            if self.local is None:
                self.status.emit("Tags der Tracks werden gelesen …")
                self.local = tag_lookup.read_local_tracks(self.folder, self.metadata_manager,
                                                          cancelled=lambda: self._cancelled)
                if self._cancelled:
                    return
                self.local_loaded.emit(self.local)
            if not self.title.strip():
                self.title, suggested_artist = tag_lookup.suggest_search(self.local)
                self.artist = self.artist or suggested_artist
            candidates = tag_lookup.search_candidates(
                self.title, self.artist, self.sources, track_count=len(self.local),
                on_status=self.status.emit, cancelled=lambda: self._cancelled)
            if not self._cancelled:
                self.completed.emit(candidates)
        except Exception as e:
            log.exception("Tag-Suche fehlgeschlagen")
            self.failed.emit(str(e))


class TagTracksWorker(QThread):
    """Laedt die Trackliste eines Kandidaten nach"""
    completed = pyqtSignal(object)  # ReleaseCandidate
    failed = pyqtSignal(str)

    def __init__(self, candidate: ReleaseCandidate):
        super().__init__()
        self.candidate = candidate

    def run(self):
        try:
            self.completed.emit(tag_lookup.load_tracks(self.candidate))
        except Exception as e:
            log.exception("Trackliste nicht ladbar")
            self.failed.emit(str(e))


class TagDiffDialog(QDialog):
    """Tags eines SD-Ordners aus MusicBrainz/Deezer/iTunes uebernehmen"""

    def __init__(self, folder, metadata_manager, parent=None, title: str = "", artist: str = "",
                 label: str = "", preset: Optional[List[ReleaseCandidate]] = None,
                 recording_ids: Optional[Dict[str, str]] = None):
        """folder: SD-Ordner (Folder) oder Liste von Dateipfaden eines Bibliotheksalbums.
        preset: Kandidaten aus der Stueck-Erkennung (dann keine automatische Suche);
        recording_ids: Dateipfad -> MusicBrainz-Recording-ID fuer die sichere Zuordnung."""
        super().__init__(parent)
        self.preset = list(preset) if preset else []
        self.recording_ids = dict(recording_ids or {})
        if isinstance(folder, (list, tuple)):
            self.setWindowTitle(f"Tags nachtragen – {label or 'Bibliotheksalbum'}")
        else:
            self.setWindowTitle(f"Tags von MusicBrainz – Ordner {folder.index:02d}")
        self.resize(1100, 640)
        self.setWindowFlags(self.windowFlags() | Qt.WindowType.WindowMaximizeButtonHint)
        self.setSizeGripEnabled(True)
        self.folder = folder
        self.metadata_manager = metadata_manager

        self.local: Optional[List[LocalTrack]] = None
        self.candidates: List[ReleaseCandidate] = []
        self.candidate: Optional[ReleaseCandidate] = None
        self.diffs: List[TrackDiff] = []
        # Ergebnis fuer den Aufrufer
        self.written = 0
        self.applied_album = ""

        self._search_worker: Optional[TagSearchWorker] = None
        self._tracks_worker: Optional[TagTracksWorker] = None
        self._updating = False

        self._setup_ui(title, artist)
        QTimer.singleShot(0, self._use_preset if self.preset else self._start_search)

    def _use_preset(self):
        """Kandidaten der Erkennung anzeigen (lokale Tags synchron lesen, keine Suche)"""
        self.status_label.setText("Tags der Tracks werden gelesen …")
        self.local = tag_lookup.read_local_tracks(self.folder, self.metadata_manager)
        if self.preset:
            self.title_edit.setText(self.preset[0].title)
            self.artist_edit.setText(self.preset[0].artist)
        # _on_candidates zeigt den ersten Kandidaten und meldet die Zuordnung (inkl. "per Erkennung")
        self._on_candidates(self.preset)

    # --- Aufbau -----------------------------------------------------------------------------

    def _setup_ui(self, title: str, artist: str):
        layout = QVBoxLayout(self)
        layout.setSpacing(12)

        form = QFormLayout()
        self.title_edit = QLineEdit(title)
        self.title_edit.setPlaceholderText("Album- bzw. Folgentitel (leer = aus den Tags ableiten)")
        self.title_edit.returnPressed.connect(self._start_search)
        form.addRow("Titel:", self.title_edit)
        self.artist_edit = QLineEdit(artist)
        self.artist_edit.setPlaceholderText("Interpret / Hörspielreihe (optional)")
        self.artist_edit.returnPressed.connect(self._start_search)
        form.addRow("Interpret:", self.artist_edit)
        layout.addLayout(form)

        saved = QSettings().value(SETTINGS_SOURCES, list(tag_lookup.ALL_SOURCES), type=list)
        sources_row = QHBoxLayout()
        sources_row.addWidget(QLabel("Quellen:"))
        self.source_checks: Dict[str, QCheckBox] = {}
        for source in tag_lookup.ALL_SOURCES:
            check = QCheckBox(source)
            check.setChecked(source in saved)
            check.setToolTip("Nur MusicBrainz liefert auch die MusicBrainz-IDs" if source == tag_lookup.SOURCE_MUSICBRAINZ
                             else f"Alben und Tracklisten bei {source} suchen")
            self.source_checks[source] = check
            sources_row.addWidget(check)
        self.btn_search = QPushButton("Suchen")
        self.btn_search.setObjectName("ghostButton")
        self.btn_search.clicked.connect(self._start_search)
        sources_row.addWidget(self.btn_search)
        sources_row.addStretch(1)
        layout.addLayout(sources_row)

        release_row = QHBoxLayout()
        release_row.addWidget(QLabel("Release:"))
        self.release_combo = QComboBox()
        self.release_combo.setMinimumWidth(500)
        self.release_combo.currentIndexChanged.connect(self._on_candidate_changed)
        release_row.addWidget(self.release_combo, 1)
        self.release_link = QLabel()
        self.release_link.setOpenExternalLinks(True)
        release_row.addWidget(self.release_link)
        layout.addLayout(release_row)

        fields_row = QHBoxLayout()
        fields_row.addWidget(QLabel("Felder:"))
        self.field_checks: Dict[str, QCheckBox] = {}
        for key, label in FIELDS:
            check = QCheckBox(label)
            check.setChecked(True)
            check.toggled.connect(lambda checked, k=key: self._toggle_column(k, checked))
            self.field_checks[key] = check
            fields_row.addWidget(check)
        fields_row.addStretch(1)
        layout.addLayout(fields_row)

        self.table = QTableWidget(0, COL_FIELD0 + len(FIELDS))
        self.table.setHorizontalHeaderLabels(["Track", "Zuordnung"] + [label for _key, label in FIELDS])
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionMode(QTableWidget.SelectionMode.NoSelection)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.setWordWrap(False)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        header.setStretchLastSection(True)
        self.table.itemChanged.connect(self._on_item_changed)
        layout.addWidget(self.table, 1)

        self.status_label = QLabel()
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)
        note = QLabel("Angehakte Felder werden in die MP3-Dateien des SD-Ordners geschrieben; "
                      "die MusicBrainz-IDs als TXXX-Tags wie bei Picard.")
        note.setObjectName("folderMeta")
        note.setWordWrap(True)
        layout.addWidget(note)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        btn_cancel = QPushButton("Abbrechen")
        btn_cancel.setObjectName("ghostButton")
        btn_cancel.clicked.connect(self.reject)
        buttons.addWidget(btn_cancel)
        self.btn_apply = QPushButton("Übernehmen")
        self.btn_apply.setObjectName("primaryButton")
        self.btn_apply.setEnabled(False)
        self.btn_apply.clicked.connect(self._apply)
        buttons.addWidget(self.btn_apply)
        layout.addLayout(buttons)

    # --- Suche ------------------------------------------------------------------------------

    def _selected_sources(self) -> List[str]:
        return [s for s, check in self.source_checks.items() if check.isChecked()]

    def _start_search(self):
        sources = self._selected_sources()
        if not sources:
            self.status_label.setText("Bitte mindestens eine Quelle wählen.")
            return
        QSettings().setValue(SETTINGS_SOURCES, sources)
        self._stop_workers()
        self.release_combo.clear()
        self.candidates = []
        self.candidate = None
        self._fill_table([])
        self._search_worker = TagSearchWorker(self.folder, self.metadata_manager, self.title_edit.text().strip(),
                                              self.artist_edit.text().strip(), sources, self.local)
        self._search_worker.local_loaded.connect(self._on_local_loaded)
        self._search_worker.status.connect(self.status_label.setText)
        self._search_worker.completed.connect(self._on_candidates)
        self._search_worker.failed.connect(lambda m: self.status_label.setText(f"Suche fehlgeschlagen: {m}"))
        self._search_worker.finished.connect(self._update_buttons)
        self._search_worker.start()
        self._update_buttons()

    def _on_local_loaded(self, local: List[LocalTrack]):
        self.local = local
        if not self.title_edit.text().strip():
            title, artist = tag_lookup.suggest_search(local)
            self.title_edit.setText(title)
            if not self.artist_edit.text().strip():
                self.artist_edit.setText(artist)

    def _on_candidates(self, candidates: List[ReleaseCandidate]):
        self.candidates = candidates
        self.release_combo.blockSignals(True)
        self.release_combo.clear()
        count = len(self.local or [])
        for candidate in candidates:
            text = candidate.label
            if count and candidate.track_count and candidate.track_count != count:
                text += f"  (hier {count})"
            self.release_combo.addItem(text)
        self.release_combo.blockSignals(False)
        if candidates:
            self.status_label.setText(f"{len(candidates)} Release(s) gefunden – passende Trackanzahl zuerst.")
            self.release_combo.setCurrentIndex(0)
            self._on_candidate_changed(0)
        else:
            self.status_label.setText("Kein Release gefunden – Titel oder Interpret anpassen und erneut suchen.")

    def _on_candidate_changed(self, index: int):
        if index < 0 or index >= len(self.candidates):
            return
        candidate = self.candidates[index]
        self.candidate = candidate
        self.release_link.setText(f'<a href="{candidate.url}">Im Browser öffnen</a>' if candidate.url else "")
        if candidate.tracks_loaded:
            self._show_candidate(candidate)
            return
        if self._tracks_worker is not None and self._tracks_worker.isRunning():
            self._tracks_worker.wait(3000)
        self.status_label.setText(f"Trackliste wird geladen ({candidate.source}) …")
        self._tracks_worker = TagTracksWorker(candidate)
        self._tracks_worker.completed.connect(self._on_tracks_loaded)
        self._tracks_worker.failed.connect(lambda m: self.status_label.setText(f"Trackliste nicht ladbar: {m}"))
        self._tracks_worker.finished.connect(self._update_buttons)
        self._tracks_worker.start()
        self._update_buttons()

    def _on_tracks_loaded(self, candidate: ReleaseCandidate):
        if candidate is self.candidate:
            self._show_candidate(candidate)

    def _show_candidate(self, candidate: ReleaseCandidate):
        if self.local is None:
            return
        self.diffs = tag_lookup.build_diff(self.local, candidate, self.recording_ids or None)
        self._fill_table(self.diffs)
        matched = sum(1 for d in self.diffs if d.matched)
        via_acoustid = sum(1 for d in self.diffs if d.method == "acoustid")
        unsure = sum(1 for d in self.diffs if d.method == tag_lookup.MATCH_POSITION)
        text = f"{matched} von {len(self.diffs)} Tracks zugeordnet"
        if via_acoustid:
            text += f" ({via_acoustid} per Erkennung)"
        if unsure:
            text += f", davon {unsure} nur über die Position – bitte prüfen"
        if candidate.source != tag_lookup.SOURCE_MUSICBRAINZ:
            text += f". {candidate.source} liefert keine MusicBrainz-IDs"
        self.status_label.setText(text + ".")
        self._update_buttons()

    # --- Tabelle ----------------------------------------------------------------------------

    def _fill_table(self, diffs: List[TrackDiff]):
        self._updating = True
        self.table.setRowCount(0)
        self.table.setRowCount(len(diffs))
        muted = QColor(theme.OVERLAY1)
        for row, diff in enumerate(diffs):
            track_item = QTableWidgetItem(diff.local.filename)
            track_item.setToolTip(diff.local.filepath)
            if diff.matched:
                track_item.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsUserCheckable)
                track_item.setCheckState(Qt.CheckState.Checked)
            else:
                track_item.setFlags(Qt.ItemFlag.ItemIsEnabled)
                track_item.setForeground(muted)
            self.table.setItem(row, COL_TRACK, track_item)

            match_item = QTableWidgetItem(diff.method or "keine")
            match_item.setFlags(Qt.ItemFlag.ItemIsEnabled)
            if diff.method in (tag_lookup.MATCH_POSITION, tag_lookup.MATCH_NONE):
                match_item.setForeground(QColor(theme.YELLOW if diff.matched else theme.RED))
            self.table.setItem(row, COL_MATCH, match_item)

            for col, (key, _label) in enumerate(FIELDS, start=COL_FIELD0):
                old, new = diff.old.get(key, ""), diff.new.get(key, "")
                item = QTableWidgetItem()
                if not diff.matched:
                    item.setText(old)
                    item.setFlags(Qt.ItemFlag.ItemIsEnabled)
                    item.setForeground(muted)
                elif diff.changed(key):
                    item.setText(f"{old} → {new}" if old else new)
                    item.setToolTip(f"Alt: {old or '–'}\nNeu: {new}")
                    item.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsUserCheckable)
                    checked = self.field_checks[key].isChecked() and not (
                        key == FIELD_MB_IDS and not diff.mb_ids)
                    item.setCheckState(Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked)
                else:
                    item.setText(new or old)
                    item.setToolTip("unverändert")
                    item.setFlags(Qt.ItemFlag.ItemIsEnabled)
                    item.setForeground(muted)
                self.table.setItem(row, col, item)
        self.table.resizeColumnsToContents()
        for col in range(self.table.columnCount()):
            self.table.setColumnWidth(col, min(self.table.columnWidth(col), 320))
        self._updating = False
        self._update_buttons()

    def _checkable(self, item: Optional[QTableWidgetItem]) -> bool:
        return item is not None and bool(item.flags() & Qt.ItemFlag.ItemIsUserCheckable)

    def _toggle_column(self, key: str, checked: bool):
        col = COL_FIELD0 + [k for k, _l in FIELDS].index(key)
        self._updating = True
        for row in range(self.table.rowCount()):
            item = self.table.item(row, col)
            if self._checkable(item):
                item.setCheckState(Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked)
        self._updating = False
        self._update_buttons()

    def _on_item_changed(self, item: QTableWidgetItem):
        if self._updating or not self._checkable(item):
            return
        if item.column() == COL_TRACK:
            self._updating = True
            state = item.checkState()
            for col in range(COL_FIELD0, self.table.columnCount()):
                cell = self.table.item(item.row(), col)
                if self._checkable(cell):
                    cell.setCheckState(state)
            self._updating = False
        self._update_buttons()

    def selection(self) -> Dict[int, List[str]]:
        """{Zeile: angehakte Felder}"""
        result: Dict[int, List[str]] = {}
        for row in range(self.table.rowCount()):
            fields = []
            for col, (key, _label) in enumerate(FIELDS, start=COL_FIELD0):
                item = self.table.item(row, col)
                if self._checkable(item) and item.checkState() == Qt.CheckState.Checked:
                    fields.append(key)
            if fields:
                result[row] = fields
        return result

    # --- Uebernehmen ------------------------------------------------------------------------

    def _apply(self):
        selection = self.selection()
        if not selection or self.candidate is None:
            return
        cells = sum(len(fields) for fields in selection.values())
        where = "die Audiodateien der Bibliothek" if isinstance(self.folder, (list, tuple)) else "die MP3-Dateien"
        if not confirm_action(self, "Tags übernehmen",
                              f"{cells} Feld(er) in {len(selection)} Track(s) werden in {where} "
                              "geschrieben. Fortfahren?", default_no=False):
            return
        parent = self.parent()
        if parent is not None and hasattr(parent, "_release_files"):
            parent._release_files()
        elif parent is not None and hasattr(parent, "_release_player"):
            parent._release_player()
        self.written = tag_lookup.apply_diff(self.diffs, selection, self.metadata_manager)
        if any(tag_lookup.FIELD_ALBUM in fields for fields in selection.values()):
            self.applied_album = self.candidate.title
        if self.written == 0:
            QMessageBox.warning(self, "Tags nicht geschrieben",
                                "Es konnte keine Datei geändert werden (gesperrt oder schreibgeschützt?).")
            return
        if self.written < len(selection):
            QMessageBox.warning(self, "Teilweise geschrieben",
                                f"Tags nur in {self.written} von {len(selection)} Tracks geschrieben.")
        self.accept()

    def _update_buttons(self, *_):
        busy = any(w is not None and w.isRunning() for w in (self._search_worker, self._tracks_worker))
        self.btn_search.setEnabled(not busy)
        self.btn_search.setText("Suche läuft …" if busy and self._search_worker is not None
                                and self._search_worker.isRunning() else "Suchen")
        self.btn_apply.setEnabled(not busy and bool(self.selection()))

    def _stop_workers(self):
        for worker in (self._search_worker, self._tracks_worker):
            if worker is not None and worker.isRunning():
                if hasattr(worker, "cancel"):
                    worker.cancel()
                worker.wait(3000)

    def done(self, result):
        self._stop_workers()
        super().done(result)
