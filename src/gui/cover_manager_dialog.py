"""
Cover-Manager: alle Cover eines Albums an einem Ort

Links grosse Kartenvorschau, darunter Aktionen und eine kompakte Galerie
(Filter: Alle / Aktuell / Online / Archiv / Neu). Rechts Tabs fuer
Cover-Suche (Deezer, iTunes, MusicBrainz, Recherche-Quellen), Kartenoptionen
und KI-Erzeugung. Jedes Bild kann als Vorlage fuer die KI markiert, als
Album-Cover (quadratisch, in die MP3s) oder als Kartencover (85,6 x 54 mm,
auf die SD-Karte) gesetzt und im Archiv behalten werden.
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
    QLineEdit, QListWidget, QListWidgetItem, QMessageBox, QPlainTextEdit, QPushButton, QSpinBox,
    QScrollArea, QSplitter, QTabBar, QTabWidget, QVBoxLayout, QWidget,
)

from core import ai_settings, album_identity, card_store, cover_sources
from core.card_text import derive_card_text, describe_work
from core.card_layout import (
    BACK_MODE_IMAGE, BACK_MODE_NONE, BACK_MODE_TRACKLIST, ORIENTATION_PORTRAIT, album_cover_jpeg, prepare_card_art,
)
from core.cover_archive import (
    KIND_AI, KIND_FILE, KIND_LOCAL, KIND_ONLINE, ROLE_ALBUM_COVER, ROLE_CARD_COVER, CoverArchive, CoverEntry, sha1,
)
from core.cover_generator import (
    DEFAULT_STYLE, ESTIMATED_COST_PER_IMAGE, MAX_REFERENCE_IMAGES, SOURCE_AI, STYLE_PRESETS, GenerationRequest,
)
from core.cover_research import CoverBrief, FolderInfo, gather_folder_info, suggest_search_title
from core.sd_card import Folder
from gui.card_cover_dialog import (
    ORIENTATIONS, _select_data, gemini_client, image_model, pil_to_pixmap, text_model,
)
from gui.common import confirm_action
from gui.tag_diff_dialog import TagDiffDialog
from gui.workers import CoverExtendWorker, CoverGenerateWorker, CoverResearchWorker

log = logging.getLogger(__name__)

SETTINGS_DATA_DIR = "library/data_dir"
SETTINGS_SOURCES = "covers/search_sources"

SECTION_CURRENT = "current"
SECTION_ONLINE = "online"
SECTION_ARCHIVE = "archive"
SECTION_NEW = "new"

# Filter der Galerie (Reihenfolge = Tabs)
FILTERS = [
    ("Alle", None),
    ("Aktuell", SECTION_CURRENT),
    ("Gefunden", SECTION_ONLINE),
    ("Archiv", SECTION_ARCHIVE),
    ("Neu", SECTION_NEW),
]


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
        box.setText("Cover-Varianten, Album-Zuordnungen und Serienprofile werden in einem "
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
    data: bytes  # Anzeige-/Vorlagenqualitaet
    kind: str
    entry: Optional[CoverEntry] = None  # im Archiv vorhanden
    online: Optional[cover_sources.OnlineCover] = None
    meta: Dict[str, str] = field(default_factory=dict)  # prompt/model/style fuer KI-Varianten
    is_sd_card_art: bool = False  # das aktuelle Kartenmotiv von der SD-Karte (card_art_raw.png)
    is_album_cover: bool = False  # das aktuell eingebettete Album-Cover
    reference: bool = False  # Vorlage fuer die KI
    back_data: Optional[bytes] = None  # zugehoerige Rueckseite (Cover Art Archive, Datei, Archiv, SD)
    _image: Optional[Image.Image] = None
    _back_image: Optional[Image.Image] = None

    @property
    def has_back(self) -> bool:
        return self.back_data is not None

    def back_image(self) -> Optional[Image.Image]:
        if self.back_data is None:
            return None
        if self._back_image is None:
            with Image.open(io.BytesIO(self.back_data)) as img:
                img.load()
                self._back_image = img.convert("RGB")
        return self._back_image

    @property
    def kept(self) -> bool:
        return self.entry is not None

    def image(self) -> Image.Image:
        if self._image is None:
            with Image.open(io.BytesIO(self.data)) as img:
                img.load()
                self._image = img.convert("RGB")
        return self._image


@dataclass
class SideSource:
    """Eine Seite eines Galerie-Bildes (Vorder- oder Rueckseite) als Bildquelle"""
    candidate: Candidate
    back: bool = False

    def image(self) -> Optional[Image.Image]:
        return self.candidate.back_image() if self.back else self.candidate.image()

    def data(self) -> Optional[bytes]:
        return self.candidate.back_data if self.back else self.candidate.data

    @property
    def label(self) -> str:
        return f"{self.candidate.label} ({'Rückseite' if self.back else 'Vorderseite'})"


def _pil_bytes(image: Image.Image) -> bytes:
    buffer = io.BytesIO()
    image.convert("RGB").save(buffer, "PNG")
    return buffer.getvalue()


class OnlineCoverSearchWorker(QThread):
    """Durchsucht die gewaehlten Quellen und meldet jedes gefundene Bild"""
    found = pyqtSignal(object, bytes)  # OnlineCover, Bilddaten
    back_found = pyqtSignal(object, bytes)  # OnlineCover, Rueckseite
    status = pyqtSignal(str)
    completed = pyqtSignal(int)

    def __init__(self, title: str, artist: str, sources: List[str], page_urls: List[str], direct_urls: List[str]):
        super().__init__()
        self.title = title
        self.artist = artist
        self.sources = sources
        self.page_urls = page_urls
        self.direct_urls = direct_urls
        self._cancelled = False

    def cancel(self):
        self._cancelled = True

    def run(self):
        try:
            count = cover_sources.search_all(
                self.title, self.artist, self.sources, self.page_urls, self.direct_urls,
                on_found=self.found.emit, on_status=self.status.emit, cancelled=lambda: self._cancelled,
                on_back=self.back_found.emit)
            self.status.emit(f"{count} Cover gefunden" if count else "Keine Cover gefunden – Suchbegriff anpassen?")
        except Exception as e:
            log.exception("Cover-Suche fehlgeschlagen")
            self.status.emit(f"Cover-Suche fehlgeschlagen: {e}")
            count = 0
        self.completed.emit(count)


# --- Dialog ----------------------------------------------------------------------------------

class CoverManagerDialog(QDialog):
    THUMB = 96

    def __init__(self, folder: Folder, folder_name: str, metadata_manager, archive: Optional[CoverArchive],
                 parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"Cover-Manager – Ordner {folder.index:02d}")
        self.setMinimumSize(980, 620)
        self.setSizeGripEnabled(True)
        geometry = QSettings().value("cover_manager/geometry")
        if geometry is not None:
            self.restoreGeometry(geometry)
        else:
            self.resize(1180, 760)
        self.folder = folder
        # "Ordner NN" ist nur der Anzeigename des Managers, kein Titel
        self.folder_name = "" if folder_name.startswith("Ordner ") else folder_name
        self.metadata_manager = metadata_manager
        self.archive = archive
        self.client = gemini_client()

        self.info = FolderInfo(album=self.folder_name)
        self.brief = CoverBrief()
        self.candidates: List[Candidate] = []
        self.selected: Optional[Candidate] = None
        self.album_id: Optional[str] = None
        self._album_tagged = False
        self._search_started = False

        self.album_cover_changed = False
        self.card_changed = False
        # Tags ueber "Tags von MusicBrainz" geschrieben: Hauptfenster frischt Ordnername und Trackliste auf
        self.tags_changed = False
        self.tags_album = ""
        self.tags_written = 0

        self._search_worker: Optional[OnlineCoverSearchWorker] = None
        self._research_worker: Optional[CoverResearchWorker] = None
        self._generate_worker: Optional[CoverGenerateWorker] = None
        self._extend_worker: Optional[CoverExtendWorker] = None
        # explizit gewaehlte Bildquellen (sonst: angeklicktes Bild bzw. dessen Rueckseite)
        self.front_source: Optional[SideSource] = None
        self.back_source: Optional[SideSource] = None

        self._card_meta = card_store.load_meta(folder.path)
        self._setup_ui()
        self._resolve_album()
        self._load_current()
        self._load_archive()
        self._update_buttons()
        QTimer.singleShot(0, self._start_research)

    # --- Aufbau -----------------------------------------------------------------------------

    def _setup_ui(self):
        outer = QHBoxLayout(self)
        self.main_splitter = QSplitter(Qt.Orientation.Horizontal)
        self.main_splitter.setChildrenCollapsible(False)
        outer.addWidget(self.main_splitter)

        # links oben: Vorschau und Aktionen; links unten: Galerie - per Splitter teilbar
        self.left_splitter = QSplitter(Qt.Orientation.Vertical)
        self.left_splitter.setChildrenCollapsible(False)
        top = QWidget()
        left = QVBoxLayout(top)
        left.setContentsMargins(0, 0, 0, 0)
        self.preview = QLabel("Bild in der Galerie auswählen")
        self.preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.preview.setMinimumSize(300, 220)
        self.preview.setStyleSheet("border: 1px dashed palette(mid); border-radius: 8px;")
        left.addWidget(self.preview, 1)

        side_row = QHBoxLayout()
        self.side_bar = QTabBar()
        self.side_bar.setDrawBase(False)
        self.side_bar.setObjectName("galleryFilter")
        self.side_bar.addTab("Vorderseite")
        self.side_bar.addTab("Rückseite")
        self.side_bar.currentChanged.connect(self._render_preview)
        side_row.addStretch(1)
        side_row.addWidget(self.side_bar)
        side_row.addStretch(1)
        left.addLayout(side_row)

        self.selection_label = QLabel()
        self.selection_label.setWordWrap(True)
        left.addWidget(self.selection_label)

        # Aktionen fuer das angeklickte (gross angezeigte) Bild - gleich gewichtet
        actions = QHBoxLayout()
        self.btn_card_cover = QPushButton("Als Vorderseite")
        self.btn_card_cover.setToolTip("Das gerade groß angezeigte Bild als Motiv der Vorderseite "
                                       "(gespeichert wird im Tab „Karte“ mit „Übernehmen“)")
        self.btn_card_cover.clicked.connect(lambda: self._pick_source(back=False))
        self.btn_back_cover = QPushButton("Als Rückseite")
        self.btn_back_cover.setToolTip("Das gerade groß angezeigte Bild als Motiv der Rückseite")
        self.btn_back_cover.clicked.connect(lambda: self._pick_source(back=True))
        self.btn_album_cover = QPushButton("Als Album-Cover verwenden")
        self.btn_album_cover.setToolTip("Quadratisch zuschneiden und in alle Tracks des Ordners schreiben")
        self.btn_album_cover.clicked.connect(self._set_album_cover)
        actions.addWidget(self.btn_card_cover)
        actions.addWidget(self.btn_back_cover)
        actions.addWidget(self.btn_album_cover)
        actions.addStretch(1)
        left.addLayout(actions)
        self.left_splitter.addWidget(top)

        bottom = QWidget()
        left = QVBoxLayout(bottom)
        left.setContentsMargins(0, 0, 0, 0)
        gallery_header = QHBoxLayout()
        self.filter_bar = QTabBar()
        self.filter_bar.setDrawBase(False)
        self.filter_bar.setObjectName("galleryFilter")
        for label, _ in FILTERS:
            self.filter_bar.addTab(label)
        self.filter_bar.currentChanged.connect(self._apply_filter)
        gallery_header.addWidget(self.filter_bar)
        gallery_header.addStretch(1)
        left.addLayout(gallery_header)

        self.gallery = QListWidget()
        self.gallery.setObjectName("coverGallery")
        self.gallery.setViewMode(QListWidget.ViewMode.IconMode)
        self.gallery.setIconSize(QSize(self.THUMB, self.THUMB))
        self.gallery.setGridSize(QSize(self.THUMB + 22, self.THUMB + 40))
        self.gallery.setResizeMode(QListWidget.ResizeMode.Adjust)
        self.gallery.setMovement(QListWidget.Movement.Static)
        self.gallery.setWordWrap(True)
        self.gallery.setMinimumHeight(self.THUMB + 54)
        self.gallery.currentItemChanged.connect(self._on_item_selected)
        self.gallery.itemChanged.connect(lambda _item: self._update_mark_bar())
        left.addWidget(self.gallery, 1)

        # Sammelaktionen fuer die markierten (abgehakten) Bilder
        mark_bar = QHBoxLayout()
        self.mark_label = QLabel()
        mark_bar.addWidget(self.mark_label)
        self.btn_keep = QPushButton("Behalten")
        self.btn_keep.setToolTip("Markierte Bilder im Archiv (Datenordner der Bibliothek) speichern")
        self.btn_keep.clicked.connect(self._keep_marked)
        self.btn_reference = QPushButton("Als KI-Vorlage")
        self.btn_reference.setToolTip("Markierte Bilder als Vorlage für die KI verwenden (bzw. wieder entfernen)")
        self.btn_reference.clicked.connect(self._toggle_reference_marked)
        self.btn_discard = QPushButton("Verwerfen")
        self.btn_discard.setObjectName("softDangerButton")
        self.btn_discard.setToolTip("Markierte Bilder entfernen – archivierte werden aus dem Archiv gelöscht")
        self.btn_discard.clicked.connect(self._discard_marked)
        self.btn_unmark = QPushButton("Keine")
        self.btn_unmark.setToolTip("Markierung aufheben")
        self.btn_unmark.setObjectName("ghostButton")
        self.btn_unmark.clicked.connect(lambda: self._set_all_marks(False))
        self.btn_mark_all = QPushButton("Alle")
        self.btn_mark_all.setToolTip("Alle sichtbaren Bilder markieren")
        self.btn_mark_all.setObjectName("ghostButton")
        self.btn_mark_all.clicked.connect(lambda: self._set_all_marks(True))
        for button in (self.btn_keep, self.btn_reference, self.btn_discard):
            mark_bar.addWidget(button)
        mark_bar.addStretch(1)
        mark_bar.addWidget(self.btn_mark_all)
        mark_bar.addWidget(self.btn_unmark)
        left.addLayout(mark_bar)

        hint = QLabel("Klick = groß anzeigen · Haken = markieren · ★ im Archiv · ○ nicht gespeichert "
                      "(verschwindet beim Schließen) · ◆ KI-Vorlage · ♪ Album-Cover · ▭ Kartencover · ⧉ mit Rückseite")
        hint.setObjectName("folderMeta")
        hint.setWordWrap(True)
        left.addWidget(hint)
        self.left_splitter.addWidget(bottom)
        self.left_splitter.setStretchFactor(0, 1)
        self.left_splitter.setStretchFactor(1, 1)
        self.main_splitter.addWidget(self.left_splitter)

        # rechts: Tabs
        right_widget = QWidget()
        right = QVBoxLayout(right_widget)
        right.setContentsMargins(0, 0, 0, 0)
        self.tabs = QTabWidget()
        self.tabs.addTab(self._search_tab(), "Cover suchen")
        self.tabs.addTab(self._card_tab(), "Karte")
        self.tabs.addTab(self._ai_tab(), "KI erzeugen")
        right.addWidget(self.tabs, 1)

        self.status_label = QLabel()
        self.status_label.setWordWrap(True)
        right.addWidget(self.status_label)

        close_row = QHBoxLayout()
        close_row.addStretch(1)
        btn_close = QPushButton("Schließen")
        btn_close.clicked.connect(self.reject)
        close_row.addWidget(btn_close)
        right.addLayout(close_row)
        right_widget.setMinimumWidth(420)
        self.left_splitter.setMinimumWidth(540)
        self.main_splitter.addWidget(right_widget)
        self.main_splitter.setStretchFactor(0, 3)
        self.main_splitter.setStretchFactor(1, 2)
        self.main_splitter.setSizes([700, 480])
        settings = QSettings()
        for splitter, key in ((self.main_splitter, "cover_manager/splitter"),
                              (self.left_splitter, "cover_manager/left_splitter")):
            state = settings.value(key)
            if state is not None:
                splitter.restoreState(state)

        if self.archive is None:
            self.status_label.setText("Kein Datenordner gewählt – Cover können nicht archiviert werden.")
        elif self.client is None:
            self.status_label.setText("Kein Gemini-API-Key hinterlegt (Kartencover → KI & Online-Dienste) – "
                                      "gefundene Cover lassen sich trotzdem als Album- oder Kartencover nutzen.")

    def _search_tab(self) -> QWidget:
        tab = QWidget()
        layout = QVBoxLayout(tab)
        form = QFormLayout()
        self.search_title_edit = QLineEdit()
        self.search_title_edit.setPlaceholderText("Album- bzw. Folgentitel")
        self.search_title_edit.returnPressed.connect(self._start_search)
        form.addRow("Titel:", self.search_title_edit)
        self.search_artist_edit = QLineEdit()
        self.search_artist_edit.setPlaceholderText("Interpret / Hörspielreihe (optional)")
        self.search_artist_edit.returnPressed.connect(self._start_search)
        form.addRow("Interpret:", self.search_artist_edit)
        layout.addLayout(form)

        saved = QSettings().value(SETTINGS_SOURCES, list(cover_sources.ALL_SOURCES), type=list)
        sources_row = QHBoxLayout()
        self.source_checks: Dict[str, QCheckBox] = {}
        for source in cover_sources.ALL_SOURCES:
            check = QCheckBox("Recherche-Quellen" if source == cover_sources.SOURCE_WEB else source)
            check.setChecked(source in saved)
            check.setToolTip("Bilder von den Seiten, die die Recherche gefunden hat"
                             if source == cover_sources.SOURCE_WEB else f"Album-Cover bei {source} suchen")
            if source == cover_sources.SOURCE_SPOTIFY and not ai_settings.get_spotify_credentials():
                check.setChecked(False)
                check.setEnabled(False)
                check.setToolTip("Spotify-Zugangsdaten unter Kartencover → KI & Online-Dienste hinterlegen")
            self.source_checks[source] = check
            sources_row.addWidget(check)
        sources_row.addStretch(1)
        layout.addLayout(sources_row)

        button_row = QHBoxLayout()
        self.btn_search = QPushButton("Cover suchen")
        self.btn_search.setObjectName("primaryButton")
        self.btn_search.clicked.connect(self._start_search)
        button_row.addWidget(self.btn_search)
        self.btn_file = QPushButton("Datei vom PC …")
        self.btn_file.setObjectName("ghostButton")
        self.btn_file.clicked.connect(self._add_file)
        button_row.addWidget(self.btn_file)
        self.btn_tags = QPushButton("Tags von MusicBrainz …")
        self.btn_tags.setObjectName("ghostButton")
        self.btn_tags.setToolTip("Album, Titel, Tracknummern und MusicBrainz-IDs der Tracks aus MusicBrainz "
                                 "(bzw. Deezer/iTunes) übernehmen - mit Prüfung alt → neu")
        self.btn_tags.clicked.connect(self._open_tag_lookup)
        button_row.addWidget(self.btn_tags)
        button_row.addStretch(1)
        layout.addLayout(button_row)

        self.search_status = QLabel()
        self.search_status.setWordWrap(True)
        layout.addWidget(self.search_status)
        note = QLabel("Gefundene Cover sind urheberrechtlich geschützt – nur für eigene Karten verwenden.")
        note.setObjectName("folderMeta")
        note.setWordWrap(True)
        layout.addWidget(note)
        layout.addStretch(1)
        return tab

    def _card_tab(self) -> QWidget:
        # Inhalt in einem Scrollbereich, damit der Tab auch bei kleinem Fenster bedienbar bleibt
        tab = QScrollArea()
        tab.setWidgetResizable(True)
        tab.setFrameShape(QScrollArea.Shape.NoFrame)
        tab.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        inner = QWidget()
        tab.setWidget(inner)
        tab_layout = QVBoxLayout(inner)
        form = QFormLayout()
        tab_layout.addLayout(form)
        tab_layout.addStretch(1)
        apply_row = QHBoxLayout()
        self.apply_note = QLabel()
        self.apply_note.setObjectName("folderMeta")
        self.apply_note.setWordWrap(True)
        apply_row.addWidget(self.apply_note, 1)
        self.btn_apply = QPushButton("Übernehmen")
        self.btn_apply.setObjectName("primaryButton")
        self.btn_apply.setToolTip("Alle Einstellungen übernehmen: Randergänzungen per KI ausführen, "
                                  "Vorder- und Rückseite auf die SD-Karte speichern")
        self.btn_apply.clicked.connect(self._apply)
        apply_row.addWidget(self.btn_apply)
        tab_layout.addLayout(apply_row)
        self.series_edit = QLineEdit()
        self.series_edit.setPlaceholderText("z.B. Die Fuchsbande · Folge 39")
        self.series_edit.textChanged.connect(self._render_preview)
        form.addRow("Reihe (klein):", self.series_edit)
        self.title_edit = QPlainTextEdit()
        self.title_edit.setPlaceholderText("Eine Zeile je Folge")
        self.title_edit.setFixedHeight(76)
        self.title_edit.textChanged.connect(self._render_preview)
        form.addRow("Titel:", self.title_edit)
        self.btn_from_tags = QPushButton("Aus Tags übernehmen")
        self.btn_from_tags.setObjectName("ghostButton")
        self.btn_from_tags.setToolTip("Reihe und Folgentitel aus den MP3-Tags (Album, Interpret) ableiten")
        self.btn_from_tags.clicked.connect(self._fill_text_from_tags)
        form.addRow("", self.btn_from_tags)
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

        front_title = QLabel("Vorderseite")
        front_title.setObjectName("sectionTitle")
        form.addRow(front_title)
        self.front_source_label = QLabel()
        self.front_source_label.setObjectName("folderMeta")
        self.front_source_label.setWordWrap(True)
        form.addRow("", self.front_source_label)
        front_buttons = QHBoxLayout()
        self.btn_front_pick = QPushButton("Bild in der Vorschau als Vorderseite")
        self.btn_front_pick.setObjectName("ghostButton")
        self.btn_front_pick.setToolTip("Das gerade groß angezeigte Bild (auch eine Rückseite) als Motiv der Vorderseite")
        self.btn_front_pick.clicked.connect(lambda: self._pick_source(back=False))
        self.btn_front_reset = QPushButton("Zurücksetzen")
        self.btn_front_reset.setObjectName("ghostButton")
        self.btn_front_reset.clicked.connect(lambda: self._set_front_source(None))
        for button in (self.btn_front_pick, self.btn_front_reset):
            front_buttons.addWidget(button)
        front_buttons.addStretch(1)
        form.addRow("", front_buttons)
        self.front_extend_check = QCheckBox("Rand per KI ergänzen (beim Übernehmen, 1 Bildaufruf)")
        self.front_extend_check.setToolTip("Das Vorderseiten-Motiv vom Bildmodell aufs Kartenformat erweitern – "
                                           "das Ergebnis erscheint als neue Version unter „Neu“")
        self.front_extend_check.toggled.connect(self._update_apply_note)
        form.addRow("", self.front_extend_check)

        back_title = QLabel("Rückseite")
        back_title.setObjectName("sectionTitle")
        form.addRow(back_title)
        self.back_mode_combo = QComboBox()
        self.back_mode_combo.addItem("Keine", BACK_MODE_NONE)
        self.back_mode_combo.addItem("Original-Rückseite / gewähltes Bild", BACK_MODE_IMAGE)
        self.back_mode_combo.addItem("Trackliste auf Bild", BACK_MODE_TRACKLIST)
        self.back_mode_combo.currentIndexChanged.connect(self._on_back_mode_changed)
        form.addRow("Rückseite:", self.back_mode_combo)
        self.back_source_label = QLabel()
        self.back_source_label.setObjectName("folderMeta")
        self.back_source_label.setWordWrap(True)
        form.addRow("", self.back_source_label)
        back_buttons = QHBoxLayout()
        self.btn_back_pick = QPushButton("Bild in der Vorschau als Rückseite")
        self.btn_back_pick.setObjectName("ghostButton")
        self.btn_back_pick.setToolTip("Das gerade groß angezeigte Bild als Rückseiten-Motiv verwenden")
        self.btn_back_pick.clicked.connect(lambda: self._pick_source(back=True))
        self.btn_back_file = QPushButton("Rückseite aus Datei …")
        self.btn_back_file.setObjectName("ghostButton")
        self.btn_back_file.clicked.connect(self._add_back_file)
        self.btn_back_reset = QPushButton("Zurücksetzen")
        self.btn_back_reset.setObjectName("ghostButton")
        self.btn_back_reset.setToolTip("Wieder die Rückseite des Vorderseiten-Covers verwenden")
        self.btn_back_reset.clicked.connect(lambda: self._set_back_source(None))
        for button in (self.btn_back_pick, self.btn_back_file, self.btn_back_reset):
            back_buttons.addWidget(button)
        back_buttons.addStretch(1)
        form.addRow("", back_buttons)
        self.back_extend_check = QCheckBox("Rand per KI ergänzen (beim Übernehmen, 1 Bildaufruf)")
        self.back_extend_check.setToolTip("Das Rückseiten-Motiv vom Bildmodell aufs Kartenformat erweitern – "
                                          "das Ergebnis erscheint als neue Version unter „Neu“")
        self.back_extend_check.toggled.connect(self._update_apply_note)
        form.addRow("", self.back_extend_check)
        self.back_heading_edit = QLineEdit()
        self.back_heading_edit.setPlaceholderText("Überschrift, z.B. Reihe · Titel")
        self.back_heading_edit.textChanged.connect(self._render_preview)
        form.addRow("Überschrift:", self.back_heading_edit)
        self.back_lines_edit = QPlainTextEdit()
        self.back_lines_edit.setPlaceholderText("Eine Zeile je Track")
        self.back_lines_edit.setFixedHeight(90)
        self.back_lines_edit.textChanged.connect(self._render_preview)
        form.addRow("Trackliste:", self.back_lines_edit)
        self.btn_back_from_tags = QPushButton("Trackliste aus Tags")
        self.btn_back_from_tags.setObjectName("ghostButton")
        self.btn_back_from_tags.clicked.connect(self._fill_back_from_tags)
        form.addRow("", self.btn_back_from_tags)

        meta = self._card_meta
        self.title_edit.setPlainText(meta.title if meta else self.folder_name)
        if meta:
            self.series_edit.setText(meta.series)
            _select_data(self.orientation_combo, meta.orientation)
            self.number_check.setChecked(meta.show_number)
            _select_data(self.back_mode_combo, meta.back_mode)
            self.back_heading_edit.setText(meta.back_heading)
            self.back_lines_edit.setPlainText("\n".join(meta.back_lines))
        self._on_back_mode_changed()
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

        self.basis_label = QLabel()
        self.basis_label.setObjectName("folderMeta")
        self.basis_label.setWordWrap(True)
        self.basis_label.setToolTip("Worauf sich Recherche und Bild-Prompt stützen")
        layout.addWidget(self.basis_label)

        header = QHBoxLayout()
        header.addWidget(QLabel("Recherche (bearbeitbar):"), 1)
        self.btn_research = QPushButton("Neu recherchieren")
        self.btn_research.setObjectName("ghostButton")
        self.btn_research.clicked.connect(self._start_research)
        header.addWidget(self.btn_research)
        layout.addLayout(header)
        self.research_edit = QPlainTextEdit()
        self.research_edit.setPlaceholderText("Recherche läuft …")
        layout.addWidget(self.research_edit, 1)
        self.sources_label = QLabel()
        self.sources_label.setOpenExternalLinks(True)
        self.sources_label.setTextFormat(Qt.TextFormat.RichText)
        self.sources_label.setWordWrap(True)
        layout.addWidget(self.sources_label)

        self.refs_label = QLabel()
        self.refs_label.setWordWrap(True)
        layout.addWidget(self.refs_label)

        row = QHBoxLayout()
        row.addWidget(QLabel("Varianten:"))
        self.count_spin = QSpinBox()
        self.count_spin.setRange(1, 4)
        self.count_spin.setValue(3)
        row.addWidget(self.count_spin)
        row.addStretch(1)
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
        tagged_id = album_identity.read_album_id(self._track_paths())
        album_id = (self._card_meta.album_id if self._card_meta else "") or tagged_id
        self._album_tagged = bool(album_id) and tagged_id == album_id
        if not album_id and self.archive is not None and self.info.album:
            record = self.archive.find_album(self.info.album)
            album_id = record.id if record else None
        self.album_id = album_id or album_identity.new_album_id()

    def _persist_album(self):
        """Album im Archiv anlegen und die SD-Tracks mit der Album-ID taggen"""
        if self.archive is None:
            return
        title = self.info.album or self.brief.album_title or self.search_title_edit.text().strip()
        self.archive.ensure_album(self.album_id, title, self.info.artist, path=self.folder.path)
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
        self.candidates.append(candidate)
        thumb = candidate.image().copy()
        thumb.thumbnail((self.THUMB * 2, self.THUMB * 2))
        item = QListWidgetItem(QIcon(pil_to_pixmap(thumb)), self._item_text(candidate))
        item.setData(Qt.ItemDataRole.UserRole, candidate)
        item.setToolTip(self._item_tooltip(candidate))
        item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
        self.gallery.blockSignals(True)
        item.setCheckState(Qt.CheckState.Unchecked)
        self.gallery.addItem(item)
        self.gallery.blockSignals(False)
        self._apply_filter()
        if select:
            if self._current_filter() not in (None, candidate.section):
                self.filter_bar.setCurrentIndex(0)
            self.gallery.setCurrentItem(item)
            self.gallery.scrollToItem(item)

    @staticmethod
    def _item_text(candidate: Candidate) -> str:
        marks = "★" if candidate.entry is not None else ("" if candidate.section == SECTION_CURRENT else "○")
        if candidate.reference:
            marks += "◆"
        if candidate.entry is not None:
            if ROLE_ALBUM_COVER in candidate.entry.roles:
                marks += "♪"
            if ROLE_CARD_COVER in candidate.entry.roles:
                marks += "▭"
        if candidate.has_back:
            marks += "⧉"
        short = candidate.online.source if candidate.online is not None else candidate.label
        return (f"{marks} {short}" if marks else short)[:28]

    @staticmethod
    def _item_tooltip(candidate: Candidate) -> str:
        width, height = candidate.image().size
        text = f"{candidate.label}\n{width} × {height} px"
        if candidate.has_back:
            text += "\nmit Rückseite"
        if candidate.online is not None and candidate.online.page_url:
            text += f"\n{candidate.online.page_url}"
        return text

    def _items(self):
        for row in range(self.gallery.count()):
            yield self.gallery.item(row)

    def _refresh_item_texts(self):
        for item in self._items():
            item.setText(self._item_text(item.data(Qt.ItemDataRole.UserRole)))

    def _current_filter(self) -> Optional[str]:
        index = self.filter_bar.currentIndex()
        return FILTERS[index][1] if 0 <= index < len(FILTERS) else None

    def _apply_filter(self, *_):
        section = self._current_filter()
        counts = {key: 0 for _, key in FILTERS}
        for item in self._items():
            candidate = item.data(Qt.ItemDataRole.UserRole)
            counts[candidate.section] = counts.get(candidate.section, 0) + 1
            item.setHidden(section is not None and candidate.section != section)
        for index, (label, key) in enumerate(FILTERS):
            count = self.gallery.count() if key is None else counts.get(key, 0)
            self.filter_bar.setTabText(index, f"{label} ({count})" if count else label)

    def _remove_candidate(self, candidate: Candidate):
        for row in range(self.gallery.count()):
            if self.gallery.item(row).data(Qt.ItemDataRole.UserRole) is candidate:
                self.gallery.takeItem(row)
                break
        self.candidates.remove(candidate)
        if self.selected is candidate:
            self.selected = None
        self._apply_filter()

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
            self._add_candidate(Candidate(SECTION_CURRENT, "Album-Cover (MP3)", cover, KIND_LOCAL,
                                          is_album_cover=True))
        raw_path = card_store.card_paths(self.folder.path)[1]
        if raw_path.exists():
            try:
                back_raw = card_store.back_paths(self.folder.path)[1]
                self._add_candidate(Candidate(SECTION_CURRENT, "Kartenmotiv (SD)", raw_path.read_bytes(),
                                              KIND_LOCAL, is_sd_card_art=True,
                                              back_data=back_raw.read_bytes() if back_raw.exists() else None),
                                    select=True)
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
            self._add_candidate(Candidate(SECTION_ARCHIVE, entry.label, data, entry.kind, entry=entry,
                                          back_data=self.archive.load_back_bytes(self.album_id, entry)))

    def _on_item_selected(self, item: Optional[QListWidgetItem], _previous=None):
        if item is None:
            return
        candidate = item.data(Qt.ItemDataRole.UserRole)
        if candidate is not self.selected:
            # Ein Klick bestimmt die Vorderseite neu. Eine explizit gewaehlte
            # Rueckseite bleibt bestehen - ausser das angeklickte Cover bringt
            # eine eigene Rueckseite mit, dann zaehlt diese.
            self.front_source = None
            if candidate.has_back:
                self.back_source = None
        self.selected = candidate
        self.selection_label.setText(self._item_tooltip(self.selected).replace("\n", " · "))
        self._update_side_controls()
        self._render_preview()
        self._update_buttons()

    def _marked_candidates(self) -> List[Candidate]:
        return [item.data(Qt.ItemDataRole.UserRole) for item in self._items()
                if item.checkState() == Qt.CheckState.Checked]

    def _references(self) -> List[Candidate]:
        return [c for c in self.candidates if c.reference]

    def _set_all_marks(self, marked: bool):
        state = Qt.CheckState.Checked if marked else Qt.CheckState.Unchecked
        self.gallery.blockSignals(True)
        for item in self._items():
            if not item.isHidden() or not marked:
                item.setCheckState(state)
        self.gallery.blockSignals(False)
        self._update_mark_bar()

    def _update_mark_bar(self):
        marked = self._marked_candidates()
        count = len(marked)
        self.mark_label.setText(f"{count} markiert:" if count else "Nichts markiert")
        keepable = [c for c in marked if not c.kept]
        discardable = [c for c in marked if c.section != SECTION_CURRENT]
        all_refs = bool(marked) and all(c.reference for c in marked)
        self.btn_keep.setText(f"Behalten ({len(keepable)})" if keepable else "Behalten")
        self.btn_keep.setEnabled(bool(keepable) and self.archive is not None)
        self.btn_reference.setText(("Keine KI-Vorlage mehr" if all_refs else "Als KI-Vorlage") +
                                   (f" ({count})" if count else ""))
        self.btn_reference.setEnabled(bool(marked))
        self.btn_discard.setText(f"Verwerfen ({len(discardable)})" if discardable else "Verwerfen")
        self.btn_discard.setEnabled(bool(discardable))
        self.btn_unmark.setEnabled(bool(marked))
        refs = len(self._references())
        text = f"Vorlagen: {refs} Bild(er) mit ◆ (max. {MAX_REFERENCE_IMAGES})"
        if refs > MAX_REFERENCE_IMAGES:
            text += " – zu viele, nur die ersten werden verwendet"
        if not refs:
            text = "Keine Vorlagen – Bilder in der Galerie markieren und „Als KI-Vorlage“ wählen."
        self.refs_label.setText(text)

    def _keep_marked(self):
        marked = [c for c in self._marked_candidates() if not c.kept]
        for candidate in marked:
            self._keep(candidate)
        self._set_all_marks(False)
        self._flash(f"✓ {len(marked)} Bild(er) im Archiv gespeichert.")

    def _toggle_reference_marked(self):
        marked = self._marked_candidates()
        new_state = not all(c.reference for c in marked)
        for candidate in marked:
            candidate.reference = new_state
        self._refresh_item_texts()
        self._set_all_marks(False)
        self._flash(f"{len(marked)} Bild(er) {'als KI-Vorlage markiert' if new_state else 'keine Vorlage mehr'}.")

    def _discard_marked(self):
        marked = [c for c in self._marked_candidates() if c.section != SECTION_CURRENT]
        archived = [c for c in marked if c.kept]
        if archived and not confirm_action(
                self, "Verwerfen", f"{len(marked)} Bild(er) verwerfen?\n\n{len(archived)} davon sind im Archiv "
                                   "gespeichert und werden dort endgültig gelöscht."):
            return
        for candidate in marked:
            if candidate.kept and self.archive is not None:
                self.archive.delete_cover(self.album_id, candidate.entry.id)
            self._remove_candidate(candidate)
        if self.selected is None:
            self.preview.clear()
            self.preview.setText("Bild in der Galerie auswählen")
            self.selection_label.clear()
        self._update_buttons()
        self._flash(f"{len(marked)} Bild(er) verworfen.")

    def _flash(self, text: str):
        """Rueckmeldung zu einer Aktion (gruen, gut sichtbar)"""
        self.status_label.setStyleSheet("color: #a6e3a1; font-weight: bold;")
        self.status_label.setText(text)
        QTimer.singleShot(6000, lambda: self.status_label.setStyleSheet(""))

    # --- Cover-Suche --------------------------------------------------------------------------

    def _selected_sources(self) -> List[str]:
        return [s for s, check in self.source_checks.items() if check.isChecked() and check.isEnabled()]

    def _start_search(self):
        title = self.search_title_edit.text().strip()
        if not title:
            self.search_status.setText("Bitte einen Album- bzw. Folgentitel eingeben.")
            return
        sources = self._selected_sources()
        QSettings().setValue(SETTINGS_SOURCES, sources)
        if self._search_worker is not None and self._search_worker.isRunning():
            self._search_worker.cancel()
            self._search_worker.wait(3000)
        for candidate in [c for c in self.candidates if c.section == SECTION_ONLINE]:
            self._remove_candidate(candidate)
        self._search_started = True
        self._search_worker = OnlineCoverSearchWorker(
            title, self.search_artist_edit.text().strip(), sources, list(self.brief.sources),
            list(self.brief.cover_urls))
        self._search_worker.found.connect(self._on_online_found)
        self._search_worker.back_found.connect(self._on_online_back_found)
        self._search_worker.status.connect(self.search_status.setText)
        self._search_worker.finished.connect(self._update_buttons)
        self._search_worker.start()
        self._update_buttons()

    def _open_tag_lookup(self):
        """Tags der Tracks aus MusicBrainz uebernehmen; danach Suchfelder und
        Kartentext aus den neuen Tags ableiten"""
        if not self.folder.tracks:
            return
        self._release_player()
        dialog = TagDiffDialog(self.folder, self.metadata_manager, self,
                               self.search_title_edit.text().strip(), self.search_artist_edit.text().strip())
        if dialog.exec() != QDialog.DialogCode.Accepted or not dialog.written:
            return
        self.tags_changed = True
        self.tags_album = dialog.applied_album or self.tags_album
        self.tags_written += dialog.written
        self.info = gather_folder_info(self.folder, self.metadata_manager)
        if self.info.album:
            self.folder_name = self.info.album
        self.search_title_edit.setText(suggest_search_title(self.info, self.brief))
        self.search_artist_edit.setText(self.info.artist or self.brief.series)
        self._fill_text_from_tags()
        self._update_basis_label()
        self.search_status.setText(f"Tags in {dialog.written} Track(s) geschrieben – Suchfelder und Kartentext "
                                   "wurden aus den neuen Tags übernommen; Recherche wird wiederholt.")
        self._start_research()

    def _on_online_found(self, cover: cover_sources.OnlineCover, data: bytes):
        self._add_candidate(Candidate(SECTION_ONLINE, cover.label, data, KIND_ONLINE, online=cover),
                            select=self.selected is None)

    def _on_online_back_found(self, cover: cover_sources.OnlineCover, data: bytes):
        for candidate in self.candidates:
            if candidate.online is cover:
                candidate.back_data = data
                candidate._back_image = None
                break
        self._refresh_item_texts()
        if self.selected is not None and self.selected.online is cover:
            self._update_side_controls()
            self._render_preview()

    def _full_resolution_back(self, candidate: Candidate) -> Optional[bytes]:
        online = candidate.online
        if candidate.back_data is None:
            return None
        if online is None or not online.back_full_url or online.back_full_url == online.back_preview_url:
            return candidate.back_data
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            return cover_sources.download_image(online.back_full_url) or candidate.back_data
        finally:
            QApplication.restoreOverrideCursor()

    def _full_resolution(self, candidate: Candidate) -> bytes:
        """Online-Bilder fuer Archiv/Verwendung in voller Groesse nachladen"""
        online = candidate.online
        if online is None or online.full_url == online.preview_url:
            return candidate.data
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            return cover_sources.download_image(online.full_url) or candidate.data
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

    def _update_basis_label(self):
        work = describe_work(self.info, self.brief)
        if work.title:
            self.basis_label.setText(f"Grundlage: {work.title} – {work.basis}")
        else:
            self.basis_label.setText("Grundlage: keine Tags – Recherche nur aus dem Ordnernamen")

    def _on_research_done(self, info: FolderInfo, brief: CoverBrief):
        self.info, self.brief = info, brief
        self._update_basis_label()
        title = suggest_search_title(info, brief)
        if not self.search_title_edit.text().strip():
            self.search_title_edit.setText(title)
        if not self.search_artist_edit.text().strip():
            self.search_artist_edit.setText(info.artist or brief.series)
        if not self._card_meta:
            self._fill_text_from_tags()
        self.research_edit.setPlainText(brief.as_text())
        if brief.sources:
            links = " · ".join(f'<a href="{u}">{i}</a>' for i, u in enumerate(brief.sources, start=1))
            self.sources_label.setText(f"Quellen: {links}")
        self.status_label.setText("Recherche fertig." if self.client else
                                  "Ohne API-Key nur Tags gelesen – Cover-Suche läuft trotzdem.")
        archived_online = any(c.kind == KIND_ONLINE for c in self.candidates if c.kept)
        if archived_online and not self._search_started:
            self.search_status.setText("Im Archiv liegen schon gefundene Cover – „Cover suchen“ sucht erneut.")
        elif not self._search_started and self.search_title_edit.text().strip():
            self._start_search()

    def _fill_text_from_tags(self):
        series, title = derive_card_text(self.info, self.brief)
        if not title:
            title = suggest_search_title(self.info, self.brief) or self.folder_name
        self.series_edit.setText(series)
        self.title_edit.setPlainText(title)

    def _start_generation(self):
        if self.client is None:
            QMessageBox.information(self, "Kein API-Key", "Bitte zuerst unter Kartencover → KI & Online-Dienste "
                                                          "einen Gemini-API-Key hinterlegen.")
            return
        refs = [c.data for c in self._references()][:MAX_REFERENCE_IMAGES]
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
        number = sum(1 for c in self.candidates if c.section == SECTION_NEW) + 1
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
        if candidate.online is not None:
            entry = self.archive.add_cover(self.album_id, self._full_resolution(candidate), KIND_ONLINE,
                                           source=candidate.online.full_url, provider=candidate.online.source)
        elif candidate.kind == KIND_AI:
            entry = self.archive.add_cover(self.album_id, candidate.image(), KIND_AI, **candidate.meta)
        else:
            entry = self.archive.add_cover(self.album_id, candidate.data, candidate.kind, source=candidate.label)
        candidate.entry = entry
        if candidate.back_data is not None and not entry.back_file:
            updated = self.archive.set_back(self.album_id, entry.id, self._full_resolution_back(candidate))
            if updated is not None:
                entry.back_file = updated.back_file
        if candidate.section in (SECTION_NEW, SECTION_ONLINE):
            candidate.section = SECTION_ARCHIVE
            self._apply_filter()
        self._refresh_item_texts()
        self._update_buttons()
        return entry

    def _ask_archive_previous(self, what: str) -> Optional[bool]:
        """True = bisheriges Bild archivieren, False = nicht, None = Abbruch"""
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Question)
        box.setWindowTitle(f"{what} ersetzen")
        box.setText(f"Das bisherige {what} ist noch nicht im Archiv. Vor dem Ersetzen ablegen?")
        btn_archive = box.addButton("Im Archiv ablegen und ersetzen", QMessageBox.ButtonRole.AcceptRole)
        btn_replace = box.addButton("Ersetzen", QMessageBox.ButtonRole.DestructiveRole)
        box.addButton("Abbrechen", QMessageBox.ButtonRole.RejectRole)
        box.setDefaultButton(btn_archive)
        box.exec()
        if box.clickedButton() == btn_archive:
            return True
        if box.clickedButton() == btn_replace:
            return False
        return None

    def _current_candidate(self, attr: str) -> Optional[Candidate]:
        return next((c for c in self.candidates if c.section == SECTION_CURRENT and getattr(c, attr)), None)

    def _archive_previous(self, current: Candidate, label: str):
        entry = self.archive.add_cover(self.album_id, current.data, KIND_LOCAL, source=label)
        archived = Candidate(SECTION_ARCHIVE, entry.label, current.data, KIND_LOCAL, entry=entry)
        self._add_candidate(archived)

    def _set_album_cover(self):
        candidate = self.selected
        if candidate is None or not self.folder.tracks or candidate.is_album_cover:
            return
        current = self._current_candidate("is_album_cover")
        if current is not None and self.archive is not None:
            record = self.archive.load_album(self.album_id)
            already = record is not None and record.album_cover_sha1 == sha1(current.data)
            if not already:
                answer = self._ask_archive_previous("Album-Cover")
                if answer is None:
                    return
                if answer:
                    self._persist_album()
                    self._archive_previous(current, "Früheres Album-Cover")

        entry = self._keep(candidate)
        source = self.archive.load_image(self.album_id, entry) if entry else candidate.image()
        jpeg = album_cover_jpeg(source)
        self._release_player()
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            updated = sum(1 for t in self.folder.tracks
                          if self.metadata_manager.set_cover_bytes(t.filepath, jpeg, "image/jpeg"))
        finally:
            QApplication.restoreOverrideCursor()
        if entry:
            self.archive.set_role(self.album_id, entry.id, ROLE_ALBUM_COVER)
            record = self.archive.load_album(self.album_id)
            if record is not None:
                record.album_cover_sha1 = sha1(jpeg)
                self.archive.save_album(record)
            self._sync_roles()

        # "Aktuell" sofort aktualisieren
        if current is not None:
            self._remove_candidate(current)
        self._add_candidate(Candidate(SECTION_CURRENT, "Album-Cover (MP3)", jpeg, KIND_LOCAL, is_album_cover=True))
        self.album_cover_changed = True
        parent = self.parent()
        if parent is not None and getattr(parent, "current_folder", None) is self.folder:
            parent._show_folder(self.folder)
        total = len(self.folder.tracks)
        if updated == total:
            self._flash(f"✓ Album-Cover in alle {total} Track(s) geschrieben.")
        else:
            QMessageBox.warning(self, "Album-Cover", f"Album-Cover nur in {updated} von {total} Track(s) "
                                                     "geschrieben – Datei gesperrt oder schreibgeschützt?")

    def _card_meta_now(self, entry: Optional[CoverEntry]) -> card_store.CardMeta:
        return card_store.CardMeta(
            title=self.title_edit.toPlainText().strip(),
            series=self.series_edit.text().strip(),
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
        front = self.front_source or (SideSource(self.selected) if self.selected is not None else None)
        if front is None:
            QMessageBox.information(self, "Keine Vorderseite", "Bitte zuerst ein Bild anklicken oder "
                                                                "„Als Vorderseite“ wählen.")
            return
        candidate = front.candidate
        # das vorhandene SD-Kartenmotiv nur neu rendern (Titel/Ausrichtung),
        # alles andere wird dabei im Archiv behalten
        current = self._current_candidate("is_sd_card_art")
        if (current is not None and current is not candidate and self.archive is not None
                and not self._card_art_archived()):
            answer = self._ask_archive_previous("Kartenmotiv")
            if answer is None:
                return
            if answer:
                self._persist_album()
                self._archive_previous(current, "Früheres Kartenmotiv")
        entry = None if front.candidate.is_sd_card_art else self._keep(front.candidate)
        if entry and not front.back:
            source = self.archive.load_image(self.album_id, entry)
        else:
            source = front.image()
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
        back_note = self._save_back(front.candidate, orientation)
        self.card_changed = True
        self._card_meta = card_store.load_meta(self.folder.path)
        if current is not candidate:
            # "Aktuell" sofort auf den neuen Stand bringen (inkl. Rueckseite)
            if current is not None:
                self._remove_candidate(current)
            back_raw = card_store.back_paths(self.folder.path)[1]
            self._add_candidate(Candidate(SECTION_CURRENT, "Kartenmotiv (SD)",
                                          card_store.card_paths(self.folder.path)[1].read_bytes(),
                                          KIND_LOCAL, is_sd_card_art=True,
                                          back_data=back_raw.read_bytes() if back_raw.exists() else None))
        self._render_preview()
        self._flash("✓ Kartencover auf der SD-Karte gespeichert (card.png)." + back_note)

    # --- Rueckseite ---------------------------------------------------------------------------

    def _on_back_mode_changed(self, *_):
        mode = self.back_mode_combo.currentData()
        is_list = mode == BACK_MODE_TRACKLIST
        for widget in (self.back_heading_edit, self.back_lines_edit, self.btn_back_from_tags):
            widget.setVisible(is_list)
        for widget in (self.btn_back_pick, self.btn_back_file, self.btn_back_reset, self.back_extend_check,
                       self.back_source_label):
            widget.setVisible(mode != BACK_MODE_NONE)
        if is_list and not self.back_lines_edit.toPlainText().strip():
            self._fill_back_from_tags()
        self._update_side_controls()
        self.side_bar.setCurrentIndex(1 if mode != BACK_MODE_NONE else 0)
        self._render_preview()

    def _fill_back_from_tags(self):
        heading = " · ".join(p for p in (self.series_edit.text().strip(),
                                         self.title_edit.toPlainText().strip().replace("\n", " / ")) if p)
        self.back_heading_edit.setText(heading)
        titles = self.info.titles or [t.display_name for t in self.folder.tracks]
        self.back_lines_edit.setPlainText("\n".join(
            f"{i:02d}. {t.split(' | ')[0]}" for i, t in enumerate(titles, start=1)))

    # --- Bildquellen der beiden Seiten --------------------------------------------------------

    def _preview_source(self) -> Optional[SideSource]:
        """Was die Vorschau gerade zeigt"""
        if self.selected is None:
            return None
        if self.side_bar.currentIndex() == 0:
            return self.front_source or SideSource(self.selected)
        if self.back_source is not None:
            return self.back_source
        if self.selected.has_back:
            return SideSource(self.selected, back=True)
        if self.back_mode_combo.currentData() == BACK_MODE_TRACKLIST:
            return SideSource(self.selected)
        return None

    def _front_image(self) -> Optional[Image.Image]:
        source = self.front_source or (SideSource(self.selected) if self.selected else None)
        return source.image() if source else None

    def _back_source_image(self, front: Optional[Candidate]) -> Optional[Image.Image]:
        """Bildquelle fuer die Rueckseite: explizit gewaehlte Seite, sonst die
        Rueckseite des Vorderseiten-Covers, bei Trackliste sonst dessen Vorderseite"""
        if self.back_source is not None:
            return self.back_source.image()
        if front is None:
            return None
        if front.has_back:
            return front.back_image()
        if self.back_mode_combo.currentData() == BACK_MODE_TRACKLIST:
            return front.image()
        return None

    def _pick_source(self, back: bool):
        """Das in der Vorschau gezeigte Bild als Vorder- bzw. Rueckseiten-Motiv uebernehmen"""
        source = self._preview_source()
        if source is None:
            return
        if back:
            if self.back_mode_combo.currentData() == BACK_MODE_NONE:
                _select_data(self.back_mode_combo, BACK_MODE_IMAGE)
            self._set_back_source(source)
        else:
            self._set_front_source(source)

    def _set_front_source(self, source: Optional[SideSource]):
        self.front_source = source
        self._update_side_controls()
        self.side_bar.setCurrentIndex(0)
        self._render_preview()

    def _set_back_source(self, source: Optional[SideSource]):
        self.back_source = source
        self._update_side_controls()
        self.side_bar.setCurrentIndex(1)
        self._render_preview()

    def _add_back_file(self):
        """Datei vom PC als Rueckseite des angeklickten Covers"""
        if self.selected is None:
            return
        path, _ = QFileDialog.getOpenFileName(self, "Rückseite auswählen", "", "Bilder (*.jpg *.jpeg *.png *.webp)")
        if not path:
            return
        try:
            data = Path(path).read_bytes()
            Image.open(io.BytesIO(data)).verify()
        except Exception as e:
            QMessageBox.warning(self, "Bild nicht lesbar", str(e))
            return
        self.selected.back_data = data
        self.selected._back_image = None
        if self.selected.entry is not None and self.archive is not None:
            self.archive.set_back(self.album_id, self.selected.entry.id, data)
        self.back_source = None
        self._refresh_item_texts()
        self._update_side_controls()
        self.side_bar.setCurrentIndex(1)
        self._render_preview()

    def _update_side_controls(self):
        mode = self.back_mode_combo.currentData()
        front = self.selected
        self.front_source_label.setText(
            f"Vorderseiten-Motiv: {self.front_source.label}" if self.front_source is not None
            else ("Vorderseiten-Motiv: das angeklickte Bild" if front is not None
                  else "Vorderseiten-Motiv: noch keins – Bild anklicken oder „Als Vorderseite“"))
        self.btn_front_reset.setEnabled(self.front_source is not None)
        preview = self._preview_source()
        self.btn_front_pick.setEnabled(preview is not None)
        self.btn_back_pick.setEnabled(preview is not None)

        if self.back_source is not None:
            text = f"Rückseiten-Motiv: {self.back_source.label}"
        elif front is not None and front.has_back:
            text = "Rückseiten-Motiv: Rückseite des angeklickten Covers"
        elif mode == BACK_MODE_TRACKLIST and front is not None:
            text = "Rückseiten-Motiv: Vorderseite des angeklickten Covers (keine eigene Rückseite vorhanden)"
        elif mode == BACK_MODE_IMAGE:
            text = "Keine Rückseite vorhanden – Bild in der Vorschau wählen oder „Rückseite aus Datei …“"
        else:
            text = ""
        self.back_source_label.setText(text)
        self.btn_back_reset.setEnabled(self.back_source is not None)
        self.btn_back_file.setEnabled(front is not None)
        self.side_bar.setTabEnabled(1, True)
        self._update_buttons()

    def _update_apply_note(self, *_):
        if not hasattr(self, "btn_apply"):
            return
        jobs = []
        if self.front_extend_check.isChecked():
            jobs.append("Vorderseite")
        if self.back_extend_check.isChecked() and self.back_mode_combo.currentData() != BACK_MODE_NONE:
            jobs.append("Rückseite")
        if jobs:
            self.apply_note.setText(f"Beim Übernehmen: Rand per KI ergänzen für {' und '.join(jobs)} "
                                    f"(ca. {len(jobs) * ESTIMATED_COST_PER_IMAGE:.2f} USD), dann speichern.")
        else:
            self.apply_note.setText("Beim Übernehmen werden Vorder- und Rückseite auf die SD-Karte gespeichert.")

    # --- Rand per KI ergaenzen --------------------------------------------------------------

    def _apply(self):
        """Alle Einstellungen uebernehmen: zuerst die angehakten Randergaenzungen
        nacheinander ausfuehren (Ergebnisse als neue Versionen unter "Neu"),
        danach Vorder- und Rueckseite speichern."""
        if self._extend_worker is not None and self._extend_worker.isRunning():
            return
        jobs = []
        if self.front_extend_check.isChecked():
            jobs.append(False)
        if self.back_extend_check.isChecked() and self.back_mode_combo.currentData() != BACK_MODE_NONE:
            jobs.append(True)
        if jobs and self.client is None:
            QMessageBox.information(self, "Kein API-Key", "Für die KI-Randergänzung bitte zuerst unter "
                                                          "Kartencover → KI & Online-Dienste einen Gemini-API-Key hinterlegen.")
            return
        self._extend_jobs = jobs
        self._run_next_extension()

    def _extension_source(self, back: bool) -> Optional[SideSource]:
        if back:
            if self.back_source is not None:
                return self.back_source
            if self.selected is not None and self.selected.has_back:
                return SideSource(self.selected, back=True)
            if self.selected is not None and self.back_mode_combo.currentData() == BACK_MODE_TRACKLIST:
                return SideSource(self.selected)
            return None
        return self.front_source or (SideSource(self.selected) if self.selected else None)

    def _run_next_extension(self):
        while self._extend_jobs:
            back = self._extend_jobs.pop(0)
            source = self._extension_source(back)
            if source is None or source.image() is None:
                continue
            self._extend_source, self._extend_back = source, back
            self.status_label.setText(f"Rand wird per KI ergänzt ({'Rückseite' if back else 'Vorderseite'}) …")
            self._extend_worker = CoverExtendWorker(self.client, image_model(), source.data(),
                                                    self.orientation_combo.currentData())
            self._extend_worker.done.connect(self._on_extended)
            self._extend_worker.failed.connect(self._on_extend_failed)
            self._extend_worker.finished.connect(self._update_buttons)
            self._extend_worker.start()
            self._update_buttons()
            return
        # alle Erweiterungen fertig (oder keine): speichern
        self.front_extend_check.setChecked(False)
        self.back_extend_check.setChecked(False)
        self._set_card_cover()

    def _on_extend_failed(self, message: str):
        QMessageBox.warning(self, "Rand nicht ergänzt", f"{message}\n\nDie Seite wird ohne Erweiterung gespeichert.")
        self._run_next_extension()

    def _on_extended(self, image: Image.Image):
        """Neue Version unter "Neu" anlegen und als Quelle der jeweiligen Seite setzen"""
        source, back = self._extend_source, self._extend_back
        base = source.candidate
        number = sum(1 for c in self.candidates if c.section == SECTION_NEW) + 1
        meta = {"style": "Rand ergänzt", "model": image_model(),
                "prompt": f"Outpainting {'Rückseite' if back else 'Vorderseite'} von {base.label}"}
        if back:
            candidate = Candidate(SECTION_NEW, f"KI-Rand · {base.label} #{number}", base.data,
                                  base.kind if base.kind != KIND_LOCAL else KIND_FILE, meta=meta,
                                  back_data=_pil_bytes(image))
            candidate._image = base.image()
            self._add_candidate(candidate)
            self.back_source = SideSource(candidate, back=True)
        else:
            candidate = Candidate(SECTION_NEW, f"KI-Rand · {base.label} #{number}", _pil_bytes(image), KIND_AI,
                                  meta=meta, back_data=base.back_data)
            candidate._image = image
            self._add_candidate(candidate)
            self.front_source = SideSource(candidate)
        self._update_side_controls()
        self._render_preview()
        self._run_next_extension()

    def _back_meta(self, meta: card_store.CardMeta, extended: bool) -> card_store.CardMeta:
        meta.back_mode = self.back_mode_combo.currentData()
        meta.back_heading = self.back_heading_edit.text().strip()
        meta.back_lines = [l for l in self.back_lines_edit.toPlainText().splitlines() if l.strip()]
        meta.back_extended = extended
        meta.back_archive_cover_id = (self.back_source.candidate.entry.id if self.back_source is not None
                                      and self.back_source.candidate.entry is not None else "")
        return meta

    def _save_back(self, front: Candidate, orientation: str) -> str:
        """Schreibt (oder entfernt) die Rueckseite passend zur Einstellung; gibt einen Hinweis zurueck"""
        mode = self.back_mode_combo.currentData()
        meta = card_store.load_meta(self.folder.path)
        if meta is None:
            return ""
        if mode == BACK_MODE_NONE:
            card_store.delete_back(self.folder.path, meta)
            return ""
        source = self._back_source_image(front)
        if source is None:
            card_store.delete_back(self.folder.path, meta)
            return " Keine Rückseite gespeichert (kein Rückseiten-Bild vorhanden)."
        extended = bool(self.back_source is not None and self.back_source.candidate.meta.get("style") == "Rand ergänzt")
        art = prepare_card_art(source, orientation)
        try:
            card_store.save_back(self.folder.path, art, self._back_meta(meta, extended))
        except OSError as e:
            return f" Rückseite nicht gespeichert: {e}"
        if front.is_sd_card_art:
            front.back_data, front._back_image = _pil_bytes(art), None
            self._refresh_item_texts()
        return " Rückseite gespeichert (card_back.png)."

    def _card_art_archived(self) -> bool:
        """Liegt das aktuelle SD-Kartenmotiv schon im Archiv?"""
        cover_id = self._card_meta.archive_cover_id if self._card_meta else ""
        return bool(cover_id) and any(e.id == cover_id for e in self.archive.covers(self.album_id))

    def _sync_roles(self):
        """Rollen aus dem Archiv in die Kandidaten uebernehmen (exklusive Rollen)"""
        if self.archive is None:
            return
        roles = {e.id: e.roles for e in self.archive.covers(self.album_id)}
        for candidate in self.candidates:
            if candidate.entry is not None and candidate.entry.id in roles:
                candidate.entry.roles = roles[candidate.entry.id]
        self._refresh_item_texts()

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
            orientation = self.orientation_combo.currentData()
            if self.side_bar.currentIndex() == 1:
                source = self._back_source_image(self.selected)
                if source is None:
                    self.preview.clear()
                    self.preview.setText("Keine Rückseite – im Tab „Karte“ unter „Rückseite“ ein Bild wählen")
                    return
                meta = self._back_meta(self._card_meta_now(None), False)
                card = card_store.render_back_from_meta(prepare_card_art(source, orientation), meta)
            else:
                art = prepare_card_art(self._front_image(), orientation)
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
        if not hasattr(self, "btn_generate"):
            return  # noch im Aufbau (Karten-Tab wird vor dem KI-Tab erstellt)
        selected = self.selected is not None
        busy = any(w is not None and w.isRunning() for w in (self._research_worker, self._generate_worker))
        searching = self._search_worker is not None and self._search_worker.isRunning()
        self.btn_album_cover.setEnabled(selected and bool(self.folder.tracks) and not self.selected.is_album_cover)
        preview = self._preview_source()
        self.btn_card_cover.setEnabled(preview is not None)
        self.btn_back_cover.setEnabled(preview is not None)
        self.btn_generate.setEnabled(not busy)
        self.btn_research.setEnabled(not busy)
        extending = self._extend_worker is not None and self._extend_worker.isRunning()
        self.btn_apply.setEnabled(not extending and (self.front_source is not None or selected))
        for check in (self.front_extend_check, self.back_extend_check):
            check.setEnabled(self.client is not None)
        self._update_apply_note()
        self.btn_search.setText("Suche läuft …" if searching else "Cover suchen")
        self.btn_search.setEnabled(not searching)
        self._update_mark_bar()

    def unsaved_variants(self) -> List[Candidate]:
        return [c for c in self.candidates if c.section == SECTION_NEW and not c.kept]

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
        settings = QSettings()
        settings.setValue("cover_manager/geometry", self.saveGeometry())
        settings.setValue("cover_manager/splitter", self.main_splitter.saveState())
        settings.setValue("cover_manager/left_splitter", self.left_splitter.saveState())
        for worker in (self._search_worker, self._research_worker, self._generate_worker, self._extend_worker):
            if worker is not None and worker.isRunning():
                if hasattr(worker, "cancel"):
                    worker.cancel()
                worker.wait(1000)
        super().done(result)
