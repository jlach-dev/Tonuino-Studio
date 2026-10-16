"""
Cover-Manager: alle Cover eines Albums an einem Ort

Links eine Galerie (Aktuell, MusicBrainz, Archiv, Neu), rechts Kartenvorschau,
Kartenoptionen und KI-Erzeugung. Jedes Bild kann als Vorlage fuer die KI
markiert, als Album-Cover (quadratisch, in die MP3s) oder als Kartencover
(85,6 x 54 mm, auf die SD-Karte) gesetzt und im Archiv behalten werden.
"""

import io
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

from PIL import Image
from PyQt6.QtCore import Qt, QSettings, QSize, QThread, QTimer, pyqtSignal
from PyQt6.QtGui import QIcon
from PyQt6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QDialog, QFileDialog, QFormLayout, QHBoxLayout, QLabel,
    QLineEdit, QListWidget, QListWidgetItem, QMessageBox, QPlainTextEdit, QPushButton, QScrollArea,
    QSpinBox, QSplitter, QTabWidget, QVBoxLayout, QWidget,
)

from core import ai_settings, album_identity, card_store, musicbrainz
from core.card_layout import ORIENTATION_PORTRAIT, album_cover_jpeg, prepare_card_art
from core.cover_archive import (
    KIND_AI, KIND_FILE, KIND_LOCAL, KIND_MUSICBRAINZ, ROLE_ALBUM_COVER, ROLE_CARD_COVER,
    CoverArchive, CoverEntry,
)
from core.cover_generator import (
    DEFAULT_STYLE, ESTIMATED_COST_PER_IMAGE, MAX_REFERENCE_IMAGES, SOURCE_AI, STYLE_PRESETS, GenerationRequest,
)
from core.cover_research import CoverBrief, FolderInfo
from core.sd_card import Folder
from gui.card_cover_dialog import (
    ORIENTATIONS, _select_data, gemini_client, image_model, pil_to_pixmap, text_model,
)
from gui.common import confirm_action
from gui.workers import CoverGenerateWorker, CoverResearchWorker

log = logging.getLogger(__name__)

SETTINGS_DATA_DIR = "library/data_dir"

SECTION_CURRENT = "current"
SECTION_MB = "mb"
SECTION_ARCHIVE = "archive"
SECTION_NEW = "new"


# --- Datenordner ----------------------------------------------------------------------------

def default_data_dir() -> str:
    drive = Path.home() / "Google Drive"
    base = drive / "Tonuino" if drive.is_dir() else Path.home() / "Documents" / "Tonuino-Manager"
    return str(base / "Bibliothek-Daten")


def get_archive(parent=None, ask: bool = True) -> Optional[CoverArchive]:
    """Cover-Archiv im Datenordner der Bibliothek; fragt beim ersten Mal nach dem Ort"""
    settings = QSettings()
    path = settings.value(SETTINGS_DATA_DIR, "", type=str)
    if not path:
        if not ask:
            return None
        suggestion = default_data_dir()
        box = QMessageBox(parent)
        box.setIcon(QMessageBox.Icon.Question)
        box.setWindowTitle("Datenordner der Bibliothek")
        box.setText("Cover-Varianten, MusicBrainz-Zuordnungen und Serienprofile werden in einem "
                    f"zentralen Datenordner gespeichert:\n\n{suggestion}")
        btn_ok = box.addButton("Übernehmen", QMessageBox.ButtonRole.AcceptRole)
        btn_other = box.addButton("Anderen Ordner wählen …", QMessageBox.ButtonRole.ActionRole)
        box.addButton("Abbrechen", QMessageBox.ButtonRole.RejectRole)
        box.exec()
        if box.clickedButton() == btn_ok:
            path = suggestion
        elif box.clickedButton() == btn_other:
            path = QFileDialog.getExistingDirectory(parent, "Datenordner wählen", str(Path(suggestion).parent))
        if not path:
            return None
        settings.setValue(SETTINGS_DATA_DIR, path)
    try:
        Path(path).mkdir(parents=True, exist_ok=True)
    except OSError as e:
        QMessageBox.warning(parent, "Datenordner", f"Datenordner nicht verfügbar:\n{path}\n\n{e}")
        return None
    return CoverArchive(path)


# --- Kandidaten -----------------------------------------------------------------------------

@dataclass
class Candidate:
    """Ein Bild in der Galerie"""
    section: str
    label: str
    data: bytes  # Anzeige-/Vorlagenqualitaet (MusicBrainz: 500 px)
    kind: str
    entry: Optional[CoverEntry] = None  # im Archiv vorhanden
    mb_image: Optional[musicbrainz.CoverArtImage] = None
    meta: Dict[str, str] = field(default_factory=dict)  # prompt/model/style fuer KI-Varianten
    is_sd_card_art: bool = False  # das aktuelle Kartenmotiv von der SD-Karte (card_art_raw.png)
    _image: Optional[Image.Image] = None

    @property
    def kept(self) -> bool:
        return self.entry is not None

    def image(self) -> Image.Image:
        if self._image is None:
            with Image.open(io.BytesIO(self.data)) as img:
                img.load()
                self._image = img.convert("RGB")
        return self._image


def _pil_bytes(image: Image.Image) -> bytes:
    buffer = io.BytesIO()
    image.convert("RGB").save(buffer, "PNG")
    return buffer.getvalue()


class MbCoverSearchWorker(QThread):
    """Sucht Release-Groups und laedt deren Cover (500 px) nacheinander"""
    found = pyqtSignal(object, bytes)  # CoverArtImage, Bilddaten
    status = pyqtSignal(str)
    completed = pyqtSignal(int)

    MAX_GROUPS = 6
    MAX_PER_GROUP = 2
    MAX_TOTAL = 12

    def __init__(self, album: str, artist: str):
        super().__init__()
        self.album = album
        self.artist = artist
        self._cancelled = False

    def cancel(self):
        self._cancelled = True

    def run(self):
        count = 0
        try:
            self.status.emit("Suche bei MusicBrainz …")
            groups = musicbrainz.search_release_groups(self.album, self.artist)
            if not groups:
                self.status.emit("MusicBrainz: nichts gefunden – Suchbegriff anpassen?")
            for group in groups[:self.MAX_GROUPS]:
                if self._cancelled or count >= self.MAX_TOTAL:
                    break
                for image in musicbrainz.list_cover_art(group)[:self.MAX_PER_GROUP]:
                    if self._cancelled or count >= self.MAX_TOTAL:
                        break
                    try:
                        self.found.emit(image, musicbrainz.fetch_image(image.url_500))
                        count += 1
                    except Exception as e:
                        log.warning("Cover %s nicht ladbar: %s", image.url_500, e)
            if groups:
                self.status.emit(f"MusicBrainz: {count} Cover aus {len(groups)} Treffer(n)")
        except Exception as e:
            log.exception("MusicBrainz-Suche fehlgeschlagen")
            self.status.emit(f"MusicBrainz-Suche fehlgeschlagen: {e}")
        self.completed.emit(count)


# --- Dialog ----------------------------------------------------------------------------------

class CoverManagerDialog(QDialog):
    PREVIEW_HEIGHT = 300
    THUMB = 110

    SECTION_TITLES = {
        SECTION_CURRENT: "Aktuell",
        SECTION_MB: "MusicBrainz / Cover Art Archive",
        SECTION_ARCHIVE: "Archiv (behaltene Cover)",
        SECTION_NEW: "Neu erzeugt (noch nicht behalten)",
    }

    def __init__(self, folder: Folder, folder_name: str, metadata_manager, archive: Optional[CoverArchive],
                 parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"Cover-Manager – Ordner {folder.index:02d}")
        self.resize(1250, 800)
        self.folder = folder
        self.folder_name = folder_name
        self.metadata_manager = metadata_manager
        self.archive = archive
        self.client = gemini_client()

        self.info = FolderInfo(album=folder_name if not folder_name.startswith("Ordner ") else "")
        self.brief = CoverBrief()
        self.candidates: Dict[str, List[Candidate]] = {s: [] for s in self.SECTION_TITLES}
        self.selected: Optional[Candidate] = None
        self.album_id: Optional[str] = None
        self._album_tagged = False

        self.album_cover_changed = False
        self.card_changed = False

        self._mb_worker: Optional[MbCoverSearchWorker] = None
        self._research_worker: Optional[CoverResearchWorker] = None
        self._generate_worker: Optional[CoverGenerateWorker] = None

        self._card_meta = card_store.load_meta(folder.path)
        self._setup_ui()
        self._resolve_album()
        self._load_current()
        self._load_archive()
        self._update_buttons()
        QTimer.singleShot(0, self._start_research)

    # --- Aufbau -----------------------------------------------------------------------------

    def _setup_ui(self):
        outer = QVBoxLayout(self)
        splitter = QSplitter(Qt.Orientation.Horizontal)
        outer.addWidget(splitter, 1)

        # Galerie
        gallery = QWidget()
        gallery_layout = QVBoxLayout(gallery)
        gallery_layout.setContentsMargins(0, 0, 8, 0)

        search_row = QHBoxLayout()
        self.mb_album_edit = QLineEdit()
        self.mb_album_edit.setPlaceholderText("Album / Hörspiel")
        self.mb_artist_edit = QLineEdit()
        self.mb_artist_edit.setPlaceholderText("Interpret (optional)")
        self.btn_mb_search = QPushButton("Bei MusicBrainz suchen")
        self.btn_mb_search.setObjectName("ghostButton")
        self.btn_mb_search.clicked.connect(self._start_mb_search)
        self.mb_album_edit.returnPressed.connect(self._start_mb_search)
        search_row.addWidget(self.mb_album_edit, 2)
        search_row.addWidget(self.mb_artist_edit, 1)
        search_row.addWidget(self.btn_mb_search)
        gallery_layout.addLayout(search_row)

        self.mb_status = QLabel()
        gallery_layout.addWidget(self.mb_status)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        sections = QWidget()
        sections_layout = QVBoxLayout(sections)
        self.lists: Dict[str, QListWidget] = {}
        self.section_labels: Dict[str, QLabel] = {}
        for section, title in self.SECTION_TITLES.items():
            label = QLabel(title)
            label.setObjectName("sectionTitle")
            sections_layout.addWidget(label)
            lst = QListWidget()
            lst.setViewMode(QListWidget.ViewMode.IconMode)
            lst.setIconSize(QSize(self.THUMB, self.THUMB))
            lst.setGridSize(QSize(self.THUMB + 24, self.THUMB + 44))
            lst.setResizeMode(QListWidget.ResizeMode.Adjust)
            lst.setMovement(QListWidget.Movement.Static)
            lst.setWordWrap(True)
            lst.setMinimumHeight(self.THUMB + 54)
            lst.setMaximumHeight(2 * (self.THUMB + 44) + 12)  # max. zwei Reihen, dann scrollen
            lst.currentItemChanged.connect(lambda cur, _prev, s=section: self._on_item_selected(s, cur))
            lst.itemChanged.connect(self._on_item_changed)
            sections_layout.addWidget(lst)
            self.lists[section] = lst
            self.section_labels[section] = label
        sections_layout.addStretch(1)
        scroll.setWidget(sections)
        gallery_layout.addWidget(scroll, 1)

        hint = QLabel("☑ = als Vorlage für die KI verwenden · ♪ = Album-Cover · ▭ = Kartencover")
        hint.setWordWrap(True)
        gallery_layout.addWidget(hint)

        actions = QHBoxLayout()
        self.btn_album_cover = QPushButton("Als Album-Cover")
        self.btn_album_cover.setToolTip("Quadratisch zuschneiden und in alle Tracks des Ordners schreiben")
        self.btn_album_cover.clicked.connect(self._set_album_cover)
        self.btn_card_cover = QPushButton("Als Kartencover")
        self.btn_card_cover.setObjectName("primaryButton")
        self.btn_card_cover.setToolTip("Mit Titel als card.png auf die SD-Karte speichern")
        self.btn_card_cover.clicked.connect(self._set_card_cover)
        self.btn_keep = QPushButton("Behalten")
        self.btn_keep.setToolTip("Im Archiv (Datenordner der Bibliothek) speichern")
        self.btn_keep.clicked.connect(lambda: self._keep(self.selected))
        self.btn_delete = QPushButton("Aus Archiv löschen")
        self.btn_delete.setObjectName("softDangerButton")
        self.btn_delete.clicked.connect(self._delete_selected)
        self.btn_file = QPushButton("Datei vom PC …")
        self.btn_file.setObjectName("ghostButton")
        self.btn_file.clicked.connect(self._add_file)
        for button in (self.btn_album_cover, self.btn_card_cover, self.btn_keep, self.btn_delete):
            actions.addWidget(button)
        actions.addStretch(1)
        actions.addWidget(self.btn_file)
        gallery_layout.addLayout(actions)
        splitter.addWidget(gallery)

        # rechte Seite: Vorschau + Tabs
        right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(8, 0, 0, 0)
        self.preview = QLabel("Bild in der Galerie auswählen")
        self.preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.preview.setMinimumHeight(self.PREVIEW_HEIGHT)
        self.preview.setStyleSheet("border: 1px dashed palette(mid); border-radius: 8px;")
        right_layout.addWidget(self.preview, 1)
        self.selection_label = QLabel()
        self.selection_label.setWordWrap(True)
        right_layout.addWidget(self.selection_label)

        tabs = QTabWidget()
        tabs.addTab(self._card_tab(), "Karte")
        tabs.addTab(self._ai_tab(), "KI erzeugen")
        right_layout.addWidget(tabs)

        self.status_label = QLabel()
        self.status_label.setWordWrap(True)
        right_layout.addWidget(self.status_label)

        close_row = QHBoxLayout()
        close_row.addStretch(1)
        btn_close = QPushButton("Schließen")
        btn_close.clicked.connect(self.reject)
        close_row.addWidget(btn_close)
        right_layout.addLayout(close_row)
        splitter.addWidget(right)
        splitter.setSizes([700, 520])

        if self.archive is None:
            self.status_label.setText("Kein Datenordner gewählt – Cover können nicht archiviert werden.")
        elif self.client is None:
            self.status_label.setText("Kein Gemini-API-Key hinterlegt (Kartencover → KI-Einstellungen) – "
                                      "Original-Cover lassen sich trotzdem als Album- oder Kartencover nutzen.")

    def _card_tab(self) -> QWidget:
        tab = QWidget()
        form = QFormLayout(tab)
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

        meta = self._card_meta
        self.title_edit.setText(meta.title if meta else self.info.album or self.folder_name)
        if meta:
            _select_data(self.orientation_combo, meta.orientation)
            self.number_check.setChecked(meta.show_number)
        return tab

    def _ai_tab(self) -> QWidget:
        tab = QWidget()
        layout = QVBoxLayout(tab)
        form = QFormLayout()
        self.style_combo = QComboBox()
        self.style_combo.addItems(list(STYLE_PRESETS))
        self.style_combo.setCurrentText(QSettings().value(ai_settings.SETTINGS_STYLE, DEFAULT_STYLE, type=str))
        form.addRow("Stil:", self.style_combo)
        self.extra_edit = QLineEdit()
        self.extra_edit.setPlaceholderText("optional, z.B. „Hexe fliegt nachts über das Dorf“")
        form.addRow("Eigener Wunsch:", self.extra_edit)
        layout.addLayout(form)

        header = QHBoxLayout()
        header.addWidget(QLabel("Recherche (bearbeitbar):"), 1)
        self.btn_research = QPushButton("Neu recherchieren")
        self.btn_research.setObjectName("ghostButton")
        self.btn_research.clicked.connect(self._start_research)
        header.addWidget(self.btn_research)
        layout.addLayout(header)
        self.research_edit = QPlainTextEdit()
        self.research_edit.setPlaceholderText("Recherche läuft …")
        self.research_edit.setMaximumHeight(130)
        layout.addWidget(self.research_edit)
        self.sources_label = QLabel()
        self.sources_label.setOpenExternalLinks(True)
        self.sources_label.setTextFormat(Qt.TextFormat.RichText)
        self.sources_label.setWordWrap(True)
        layout.addWidget(self.sources_label)

        row = QHBoxLayout()
        self.refs_label = QLabel()
        row.addWidget(self.refs_label, 1)
        row.addWidget(QLabel("Varianten:"))
        self.count_spin = QSpinBox()
        self.count_spin.setRange(1, 4)
        self.count_spin.setValue(3)
        row.addWidget(self.count_spin)
        self.btn_generate = QPushButton("Motive erzeugen")
        self.btn_generate.setObjectName("primaryButton")
        self.btn_generate.clicked.connect(self._start_generation)
        row.addWidget(self.btn_generate)
        layout.addLayout(row)
        return tab

    # --- Album-Zuordnung ----------------------------------------------------------------------

    def _track_paths(self) -> List[str]:
        return [t.filepath for t in self.folder.tracks]

    def _resolve_album(self):
        """Album-ID aus card.json bzw. ID-Tag der Tracks; sonst Treffer im
        Archiv ueber den Titel; sonst neu (wird erst beim ersten Speichern
        dauerhaft angelegt)."""
        album_id = (self._card_meta.album_id if self._card_meta else "") or \
            album_identity.read_album_id(self._track_paths())
        self._album_tagged = bool(album_id) and album_identity.read_album_id(self._track_paths()) == album_id
        if not album_id and self.archive is not None and self.info.album:
            record = self.archive.find_album(self.info.album)
            album_id = record.id if record else None
        self.album_id = album_id or album_identity.new_album_id()

    def _persist_album(self):
        """Album im Archiv anlegen und die SD-Tracks mit der Album-ID taggen"""
        if self.archive is None:
            return
        self.archive.ensure_album(self.album_id, self.info.album or self.folder_name, self.info.artist,
                                  path=self.folder.path)
        if not self._album_tagged and self.folder.tracks:
            self._release_player()
            album_identity.write_album_id(self._track_paths(), self.album_id)
            self._album_tagged = True

    def _release_player(self):
        parent = self.parent()
        if parent is not None and hasattr(parent, "_release_files"):
            parent._release_files()

    # --- Galerie ------------------------------------------------------------------------------

    def _add_candidate(self, candidate: Candidate, select: bool = False):
        self.candidates[candidate.section].append(candidate)
        lst = self.lists[candidate.section]
        thumb = candidate.image().copy()
        thumb.thumbnail((self.THUMB * 2, self.THUMB * 2))
        item = QListWidgetItem(QIcon(pil_to_pixmap(thumb)), self._item_text(candidate))
        item.setData(Qt.ItemDataRole.UserRole, candidate)
        item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
        lst.blockSignals(True)
        item.setCheckState(Qt.CheckState.Unchecked)
        lst.addItem(item)
        lst.blockSignals(False)
        self._update_section_visibility()
        if select:
            lst.setCurrentItem(item)
            lst.scrollToItem(item)

    @staticmethod
    def _item_text(candidate: Candidate) -> str:
        marks = ""
        if candidate.entry is not None:
            if ROLE_ALBUM_COVER in candidate.entry.roles:
                marks += "♪ "
            if ROLE_CARD_COVER in candidate.entry.roles:
                marks += "▭ "
        return (marks + candidate.label)[:60]

    def _refresh_item_texts(self):
        for lst in self.lists.values():
            for row in range(lst.count()):
                item = lst.item(row)
                item.setText(self._item_text(item.data(Qt.ItemDataRole.UserRole)))

    def _update_section_visibility(self):
        for section, lst in self.lists.items():
            visible = lst.count() > 0 or section == SECTION_MB
            lst.setVisible(visible)
            self.section_labels[section].setVisible(visible)

    def _remove_candidate(self, candidate: Candidate):
        lst = self.lists[candidate.section]
        for row in range(lst.count()):
            if lst.item(row).data(Qt.ItemDataRole.UserRole) is candidate:
                lst.takeItem(row)
                break
        self.candidates[candidate.section].remove(candidate)
        if self.selected is candidate:
            self.selected = None
        self._update_section_visibility()

    def _load_current(self):
        cover = None
        for track in self.folder.tracks[:3]:
            cover = self.metadata_manager.get_cover_bytes(track.filepath)
            if cover:
                break
        if not cover and self.folder.cover_path:
            try:
                cover = Path(self.folder.cover_path).read_bytes()
            except OSError:
                cover = None
        if cover:
            self._add_candidate(Candidate(SECTION_CURRENT, "Album-Cover (MP3)", cover, KIND_LOCAL))
        raw_path = card_store.card_paths(self.folder.path)[1]
        if raw_path.exists():
            try:
                self._add_candidate(Candidate(SECTION_CURRENT, "Kartenmotiv (SD)", raw_path.read_bytes(),
                                              KIND_LOCAL, is_sd_card_art=True), select=True)
            except OSError:
                pass

    def _load_archive(self):
        if self.archive is None:
            return
        for entry in self.archive.covers(self.album_id):
            try:
                data = self.archive.load_bytes(self.album_id, entry)
            except OSError:
                continue
            self._add_candidate(Candidate(SECTION_ARCHIVE, entry.label, data, entry.kind, entry=entry))

    def _on_item_selected(self, section: str, item: Optional[QListWidgetItem]):
        if item is None:
            return
        for other, lst in self.lists.items():
            if other != section and lst.currentItem() is not None:
                lst.blockSignals(True)
                lst.setCurrentItem(None)
                lst.clearSelection()
                lst.blockSignals(False)
        self.selected = item.data(Qt.ItemDataRole.UserRole)
        source = self.selected.label
        if self.selected.mb_image is not None:
            source += f" – {self.selected.mb_image.url_500}"
        self.selection_label.setText(f"Auswahl: {source}")
        self._render_preview()
        self._update_buttons()

    def _on_item_changed(self, _item):
        self._update_refs_label()

    def _checked_candidates(self) -> List[Candidate]:
        result = []
        for lst in self.lists.values():
            for row in range(lst.count()):
                item = lst.item(row)
                if item.checkState() == Qt.CheckState.Checked:
                    result.append(item.data(Qt.ItemDataRole.UserRole))
        return result

    def _update_refs_label(self):
        count = len(self._checked_candidates())
        text = f"Vorlagen: {count}/{MAX_REFERENCE_IMAGES}"
        if count > MAX_REFERENCE_IMAGES:
            text += " – zu viele, nur die ersten werden verwendet"
        self.refs_label.setText(text)

    # --- MusicBrainz --------------------------------------------------------------------------

    def _start_mb_search(self):
        album = self.mb_album_edit.text().strip()
        if not album:
            self.mb_status.setText("Bitte einen Album- bzw. Hörspieltitel eingeben.")
            return
        if self._mb_worker is not None and self._mb_worker.isRunning():
            self._mb_worker.cancel()
            self._mb_worker.wait(2000)
        for candidate in list(self.candidates[SECTION_MB]):
            self._remove_candidate(candidate)
        self._mb_worker = MbCoverSearchWorker(album, self.mb_artist_edit.text().strip())
        self._mb_worker.found.connect(self._on_mb_found)
        self._mb_worker.status.connect(self.mb_status.setText)
        self._mb_worker.finished.connect(self._update_buttons)
        self._mb_worker.start()
        self._update_buttons()

    def _on_mb_found(self, image: musicbrainz.CoverArtImage, data: bytes):
        label = image.release_group.label + ("" if image.front else " (Rückseite/Inlay)")
        self._add_candidate(Candidate(SECTION_MB, label, data, KIND_MUSICBRAINZ, mb_image=image))

    def _full_resolution(self, candidate: Candidate) -> bytes:
        """MusicBrainz-Bilder fuer Archiv/Verwendung in 1200 px nachladen"""
        if candidate.mb_image is None or candidate.mb_image.url_1200 == candidate.mb_image.url_500:
            return candidate.data
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            return musicbrainz.fetch_image(candidate.mb_image.url_1200)
        except Exception as e:
            log.warning("1200-px-Version nicht ladbar, nehme 500 px: %s", e)
            return candidate.data
        finally:
            QApplication.restoreOverrideCursor()

    # --- Recherche & Generierung --------------------------------------------------------------

    def _start_research(self):
        if self._research_worker is not None and self._research_worker.isRunning():
            return
        self.status_label.setText("Recherche läuft …")
        self._research_worker = CoverResearchWorker(self.folder, self.metadata_manager, self.client, text_model(),
                                                    self.extra_edit.text(), online_cover=False)
        self._research_worker.completed.connect(self._on_research_done)
        self._research_worker.failed.connect(lambda m: self.status_label.setText(f"Recherche fehlgeschlagen: {m}"))
        self._research_worker.finished.connect(self._update_buttons)
        self._research_worker.start()
        self._update_buttons()

    def _on_research_done(self, info: FolderInfo, brief: CoverBrief):
        self.info, self.brief = info, brief
        if not self.mb_album_edit.text():
            self.mb_album_edit.setText(info.album or self.folder_name)
            self.mb_artist_edit.setText(info.artist)
            self._start_mb_search()
        if not self._card_meta and (not self.title_edit.text() or self.title_edit.text().startswith("Ordner ")):
            self.title_edit.setText(info.album)
        self.research_edit.setPlainText(brief.as_text())
        if brief.sources:
            links = " · ".join(f'<a href="{u}">{i}</a>' for i, u in enumerate(brief.sources, start=1))
            self.sources_label.setText(f"Quellen: {links}")
        self.status_label.setText("Recherche fertig." if self.client else
                                  "Ohne API-Key nur Tags gelesen; Original-Cover über MusicBrainz.")

    def _start_generation(self):
        if self.client is None:
            QMessageBox.information(self, "Kein API-Key", "Bitte zuerst unter Kartencover → KI-Einstellungen "
                                                          "einen Gemini-API-Key hinterlegen.")
            return
        refs = [c.data for c in self._checked_candidates()][:MAX_REFERENCE_IMAGES]
        count = self.count_spin.value()
        request = GenerationRequest(
            info=self.info, brief=self.brief, brief_text=self.research_edit.toPlainText(),
            style=self.style_combo.currentText(), extra=self.extra_edit.text(),
            orientation=self.orientation_combo.currentData(), use_reference=bool(refs), reference_images=refs)
        self._generation_meta = {"style": self.style_combo.currentText(), "model": image_model(),
                                 "prompt": self.extra_edit.text()}
        self.status_label.setText(f"Erzeuge {count} Motiv(e) mit {len(refs)} Vorlage(n) … "
                                  f"(ca. {count * ESTIMATED_COST_PER_IMAGE:.2f} USD)")
        self._generate_worker = CoverGenerateWorker(SOURCE_AI, request, self.client, image_model(), count)
        self._generate_worker.variant_ready.connect(self._on_variant_ready)
        self._generate_worker.failed.connect(lambda m: QMessageBox.warning(self, "Motiv nicht erzeugt", m))
        self._generate_worker.completed.connect(
            lambda: self.status_label.setText("Fertig – Varianten prüfen und gewünschte behalten."))
        self._generate_worker.finished.connect(self._update_buttons)
        self._generate_worker.start()
        self._update_buttons()

    def _on_variant_ready(self, art: Image.Image):
        number = len(self.candidates[SECTION_NEW]) + 1
        candidate = Candidate(SECTION_NEW, f"KI · {self._generation_meta['style']} #{number}", _pil_bytes(art),
                              KIND_AI, meta=dict(self._generation_meta))
        candidate._image = art
        self._add_candidate(candidate, select=True)

    # --- Aktionen -----------------------------------------------------------------------------

    def _keep(self, candidate: Optional[Candidate]) -> Optional[CoverEntry]:
        """Legt das Bild im Archiv ab (falls noch nicht geschehen)"""
        if candidate is None or self.archive is None:
            return None
        if candidate.entry is not None:
            return candidate.entry
        self._persist_album()
        if candidate.kind == KIND_MUSICBRAINZ and candidate.mb_image is not None:
            data = self._full_resolution(candidate)
            entry = self.archive.add_cover(self.album_id, data, KIND_MUSICBRAINZ, source=candidate.mb_image.url_1200)
        elif candidate.kind == KIND_AI:
            entry = self.archive.add_cover(self.album_id, candidate.image(), KIND_AI, **candidate.meta)
        else:
            entry = self.archive.add_cover(self.album_id, candidate.data, candidate.kind, source=candidate.label)
        candidate.entry = entry
        if candidate.section == SECTION_NEW:
            # behaltene Varianten wandern optisch ins Archiv
            self._remove_candidate(candidate)
            candidate.section = SECTION_ARCHIVE
            candidate.label = entry.label
            self._add_candidate(candidate, select=True)
        self._refresh_item_texts()
        self._update_buttons()
        return entry

    def _set_album_cover(self):
        candidate = self.selected
        if candidate is None or not self.folder.tracks:
            return
        entry = self._keep(candidate)
        source = self.archive.load_image(self.album_id, entry) if entry else candidate.image()
        jpeg = album_cover_jpeg(source)
        self._release_player()
        updated = sum(1 for t in self.folder.tracks
                      if self.metadata_manager.set_cover_bytes(t.filepath, jpeg, "image/jpeg"))
        if entry:
            self.archive.set_role(self.album_id, entry.id, ROLE_ALBUM_COVER)
            self._sync_roles()
        self.album_cover_changed = True
        self.status_label.setText(f"Album-Cover in {updated} Track(s) geschrieben.")

    def _card_meta_now(self, entry: Optional[CoverEntry]) -> card_store.CardMeta:
        return card_store.CardMeta(
            title=self.title_edit.text().strip(),
            orientation=self.orientation_combo.currentData(),
            show_number=self.number_check.isChecked(),
            source=entry.kind if entry else "",
            style=entry.style if entry else "",
            prompt=entry.prompt if entry else "",
            model=entry.model if entry else "",
            research_sources=list(self.brief.sources),
            album_id=self.album_id if self.archive is not None else "",
            archive_cover_id=entry.id if entry else "",
        )

    def _set_card_cover(self):
        candidate = self.selected
        if candidate is None:
            return
        # das vorhandene SD-Kartenmotiv nur neu rendern (Titel/Ausrichtung),
        # alles andere wird dabei im Archiv behalten
        entry = None if candidate.is_sd_card_art else self._keep(candidate)
        source = self.archive.load_image(self.album_id, entry) if entry else candidate.image()
        orientation = self.orientation_combo.currentData()
        art = prepare_card_art(source, orientation)
        QSettings().setValue(ai_settings.SETTINGS_ORIENTATION, orientation)
        QSettings().setValue(ai_settings.SETTINGS_STYLE, self.style_combo.currentText())
        try:
            card_store.save_card(self.folder.path, art, self._card_meta_now(entry), self.folder.index)
        except OSError as e:
            QMessageBox.warning(self, "Speichern fehlgeschlagen", str(e))
            return
        self.folder.card_path = str(card_store.card_paths(self.folder.path)[0])
        if entry:
            self.archive.set_role(self.album_id, entry.id, ROLE_CARD_COVER)
            self._sync_roles()
        self.card_changed = True
        self.status_label.setText("Kartencover auf der SD-Karte gespeichert.")

    def _sync_roles(self):
        """Rollen aus dem Archiv in die Kandidaten uebernehmen (exklusive Rollen)"""
        if self.archive is None:
            return
        roles = {e.id: e.roles for e in self.archive.covers(self.album_id)}
        for candidates in self.candidates.values():
            for candidate in candidates:
                if candidate.entry is not None and candidate.entry.id in roles:
                    candidate.entry.roles = roles[candidate.entry.id]
        self._refresh_item_texts()

    def _delete_selected(self):
        candidate = self.selected
        if candidate is None or candidate.entry is None or self.archive is None:
            return
        if not confirm_action(self, "Aus Archiv löschen", "Dieses Cover endgültig aus dem Archiv löschen?"):
            return
        self.archive.delete_cover(self.album_id, candidate.entry.id)
        self._remove_candidate(candidate)
        self.preview.clear()
        self.preview.setText("Bild in der Galerie auswählen")
        self._update_buttons()

    def _add_file(self):
        path, _ = QFileDialog.getOpenFileName(self, "Bild auswählen", "", "Bilder (*.jpg *.jpeg *.png *.webp);;Alle (*)")
        if not path:
            return
        try:
            data = Path(path).read_bytes()
            Image.open(io.BytesIO(data)).verify()
        except Exception as e:
            QMessageBox.warning(self, "Bild nicht lesbar", str(e))
            return
        candidate = Candidate(SECTION_NEW, os.path.basename(path), data, KIND_FILE)
        self._add_candidate(candidate, select=True)
        if self.archive is not None:
            self._keep(candidate)

    # --- Vorschau / Zustand -------------------------------------------------------------------

    def _render_preview(self, *_):
        if self.selected is None:
            return
        try:
            art = prepare_card_art(self.selected.image(), self.orientation_combo.currentData())
            card = card_store.render_from_meta(art, self._card_meta_now(None), self.folder.index)
        except Exception as e:
            self.preview.setText(f"Vorschau nicht möglich: {e}")
            return
        pixmap = pil_to_pixmap(card)
        self.preview.setPixmap(pixmap.scaled(self.preview.size() - QSize(16, 16), Qt.AspectRatioMode.KeepAspectRatio,
                                             Qt.TransformationMode.SmoothTransformation))

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._render_preview()

    def _update_buttons(self, *_):
        selected = self.selected is not None
        has_archive = self.archive is not None
        busy = any(w is not None and w.isRunning() for w in (self._research_worker, self._generate_worker))
        self.btn_album_cover.setEnabled(selected and bool(self.folder.tracks))
        self.btn_card_cover.setEnabled(selected)
        self.btn_keep.setEnabled(selected and has_archive and not self.selected.kept)
        self.btn_delete.setEnabled(selected and has_archive and self.selected.kept)
        self.btn_file.setEnabled(True)
        self.btn_generate.setEnabled(not busy)
        self.btn_research.setEnabled(not busy)
        mb_busy = self._mb_worker is not None and self._mb_worker.isRunning()
        self.btn_mb_search.setEnabled(not mb_busy)
        self._update_refs_label()

    def unsaved_variants(self) -> List[Candidate]:
        return [c for c in self.candidates[SECTION_NEW] if not c.kept]

    def done(self, result):
        unsaved = self.unsaved_variants()
        if unsaved and self.archive is not None:
            box = QMessageBox(self)
            box.setIcon(QMessageBox.Icon.Question)
            box.setWindowTitle("Nicht behaltene Varianten")
            box.setText(f"{len(unsaved)} neu erzeugte Variante(n) wurden nicht behalten und gehen beim "
                        "Schließen verloren.")
            btn_discard = box.addButton("Verwerfen", QMessageBox.ButtonRole.DestructiveRole)
            btn_keep_all = box.addButton("Alle behalten", QMessageBox.ButtonRole.AcceptRole)
            box.addButton("Zurück", QMessageBox.ButtonRole.RejectRole)
            box.exec()
            if box.clickedButton() == btn_keep_all:
                for candidate in list(unsaved):
                    self._keep(candidate)
            elif box.clickedButton() != btn_discard:
                return
        for worker in (self._mb_worker, self._research_worker, self._generate_worker):
            if worker is not None and worker.isRunning():
                if hasattr(worker, "cancel"):
                    worker.cancel()
                worker.wait(1000)
        super().done(result)
