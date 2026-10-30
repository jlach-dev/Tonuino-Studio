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
import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

from PIL import Image
from PyQt6.QtCore import Qt, QSettings, QSize, QThread, QTimer, pyqtSignal
from PyQt6.QtGui import QIcon
from PyQt6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QDialog, QFileDialog, QFormLayout, QHBoxLayout, QInputDialog, QLabel,
    QLineEdit, QListWidget, QListWidgetItem, QMenu, QMessageBox, QPlainTextEdit, QPushButton, QSpinBox,
    QScrollArea, QSplitter, QTabBar, QTabWidget, QVBoxLayout, QWidget,
)

from core import ai_settings, album_identity, card_store, cover_sources
from core.card_text import derive_card_text, describe_work
from core.card_layout import (
    BACK_MODE_AI, BACK_MODE_BLUR, BACK_MODE_GRADIENT, BACK_MODE_IMAGE, BACK_MODE_NONE, BACK_MODE_TRACKLIST,
    CardSpec, ORIENTATION_PORTRAIT, TextStyle, album_cover_jpeg, blur_background, gradient_background,
    fits_card_format, prepare_card_art,
)
from core.cover_archive import (
    KIND_AI, KIND_FILE, KIND_LOCAL, KIND_ONLINE, ROLE_ALBUM_COVER, ROLE_CARD_COVER, ROLE_SERIES_ANCHOR,
    CoverArchive, CoverEntry, sha1,
)
from core.series import MAX_ANCHORS, SIDE_BACK, SIDE_FRONT, AnchorRef, SeriesProfile, load_anchor_bytes
from core.cover_generator import (
    DEFAULT_STYLE, ESTIMATED_COST_PER_IMAGE, MAX_REFERENCE_IMAGES, SOURCE_AI, STYLE_PRESETS, GenerationRequest,
)
from core.cover_research import CoverBrief, FolderInfo, gather_folder_info, suggest_search_title
from core.sd_card import Folder
from gui.card_cover_dialog import (
    ORIENTATIONS, _select_data, gemini_client, image_model, pil_to_pixmap, text_model,
)
from gui.common import confirm_action, icon_from_glyph
from gui.text_style_dialog import TextStylesPopup
from gui import theme
from gui.tag_diff_dialog import TagDiffDialog
from gui.crash_handler import guarded_slot
from gui.library_settings_dialog import library_id_tags_allowed
from gui.workers import (
    BackIdeasWorker, CharacterSheetWorker, CoverBackGenerateWorker, CoverExtendWorker, CoverGenerateWorker,
    CoverResearchWorker, SeriesDetectWorker, StyleBibleWorker, retire_worker,
)

log = logging.getLogger(__name__)

SETTINGS_DATA_DIR = "library/data_dir"
SETTINGS_SOURCES = "covers/search_sources"
SETTINGS_TITLE_STYLE = "covers/title_style"
SETTINGS_SERIES_STYLE = "covers/series_style"

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

    FOCUS_CARD = "card"
    FOCUS_ALBUM = "album"

    def __init__(self, folder: Folder, folder_name: str, metadata_manager, archive: Optional[CoverArchive],
                 parent=None, focus: str = "card", library: bool = False, album_id: str = ""):
        """library=True: der 'Ordner' ist ein Bibliotheksalbum (folder.index None).
        Das Album-Cover wird dann in die Bibliotheksdateien und als cover.jpg
        geschrieben; Kartendateien landen im Archiv statt auf einer SD-Karte."""
        super().__init__(parent)
        self.focus = focus
        self.library_mode = library
        self._album_id_hint = album_id
        where = f"Bibliothek: {folder_name}" if library else f"Ordner {folder.index:02d}"
        self.setWindowTitle(f"Cover-Manager – Album-Cover & Kartencover – {where}")
        # Wie das Hauptfenster: maximierbar (Dialoge haben den Knopf sonst nicht)
        self.setWindowFlags(self.windowFlags() | Qt.WindowType.WindowMaximizeButtonHint
                            | Qt.WindowType.WindowMinimizeButtonHint)
        self.setMinimumSize(980, 620)
        self.setSizeGripEnabled(True)
        settings = QSettings()
        geometry = settings.value("cover_manager/geometry")
        if geometry is not None:
            self.restoreGeometry(geometry)
        else:
            self.resize(1180, 760)
        if settings.value("cover_manager/maximized", False, type=bool):
            self.setWindowState(Qt.WindowState.WindowMaximized)
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
        self._back_worker: Optional[CoverBackGenerateWorker] = None
        self._ideas_worker: Optional[BackIdeasWorker] = None
        self._series_detect_worker: Optional[SeriesDetectWorker] = None
        self._bible_worker: Optional[StyleBibleWorker] = None
        self._sheet_worker: Optional[CharacterSheetWorker] = None
        # Serie (core/series.py): zugeordnetes Profil, erkannte Reihe, geladene Anker-Bilder
        self.series_profile: Optional[SeriesProfile] = None
        self._detected_series = ("", "")  # Name, MusicBrainz-Serien-ID
        self._anchor_images: List[bytes] = []
        self._extend_jobs: list = []  # anstehende KI-Schritte beim Uebernehmen
        # explizit gewaehlte Bildquellen (sonst: angeklicktes Bild bzw. dessen Rueckseite)
        self.front_source: Optional[SideSource] = None
        self.back_source: Optional[SideSource] = None
        # zuletzt erzeugte KI-Rueckseite - unabhaengig davon, welches Bild angeklickt ist
        self.ai_back: Optional[SideSource] = None

        # Kartendateien: auf der SD-Karte im Ordner, aus der Bibliothek heraus im Archiv
        self._card_dir = folder.path
        self._card_meta = None if library else card_store.load_meta(folder.path)
        self._setup_ui()
        self._resolve_album()
        if library and self.archive is not None:
            self._card_dir = str(self.archive.card_dir(self.album_id))
            self._card_meta = card_store.load_meta(self._card_dir)
            if self._card_meta is not None:
                self._apply_meta_to_ui(self._card_meta)
        self._load_current()
        self._load_archive()
        self._load_series()
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
        # Was beim Uebernehmen gespeichert wird - in jedem Reiter sichtbar
        self.sides_label = QLabel()
        self.sides_label.setObjectName("folderMeta")
        self.sides_label.setWordWrap(True)
        left.addWidget(self.sides_label)

        # Aktionen fuer das angeklickte (gross angezeigte) Bild - gleich gewichtet
        # Zwei getrennte Aufgaben: Album-Cover (in den MP3s) und Kartencover (Druck)
        actions = QHBoxLayout()
        actions.setSpacing(18)
        album_box = QVBoxLayout()
        album_box.setSpacing(4)
        album_caption = QLabel("Album-Cover (in den MP3s)")
        album_caption.setObjectName("folderMeta")
        album_caption.setToolTip("Wird in alle Tracks des Ordners geschrieben und in der App angezeigt")
        album_box.addWidget(album_caption)
        self.btn_album_cover = QPushButton("Als Album-Cover setzen")
        self.btn_album_cover.setToolTip("Das gerade groß angezeigte Bild quadratisch zuschneiden und in alle "
                                        "Tracks des Ordners schreiben – sofort wirksam")
        self.btn_album_cover.clicked.connect(self._set_album_cover)
        album_box.addWidget(self.btn_album_cover, 0, Qt.AlignmentFlag.AlignLeft)
        actions.addLayout(album_box)

        card_box = QVBoxLayout()
        card_box.setSpacing(4)
        card_caption = QLabel("Kartencover (Druck, Tab „Karte“)")
        card_caption.setObjectName("folderMeta")
        card_caption.setToolTip("Motiv für die gedruckte RFID-Karte; gespeichert wird im Tab „Karte“ mit „Übernehmen“")
        card_box.addWidget(card_caption)
        card_buttons = QHBoxLayout()
        self.btn_card_cover = QPushButton("Als Vorderseite")
        self.btn_card_cover.setToolTip("Das angeklickte Bild (bzw. seine Rückseite, wenn „Rückseite“ gewählt ist) "
                                       "als Motiv der Vorderseite – gespeichert wird im Tab „Karte“ mit „Übernehmen“")
        self.btn_card_cover.clicked.connect(lambda: self._pick_source(back=False))
        self.btn_back_cover = QPushButton("Als Rückseite")
        self.btn_back_cover.setToolTip("Das angeklickte Bild (bzw. seine Rückseite) als Motiv der Rückseite; "
                                       "der Rückseiten-Modus wechselt dabei auf „Bild“")
        self.btn_back_cover.clicked.connect(lambda: self._pick_source(back=True))
        card_buttons.addWidget(self.btn_card_cover)
        card_buttons.addWidget(self.btn_back_cover)
        card_buttons.addStretch(1)
        card_box.addLayout(card_buttons)
        actions.addLayout(card_box, 1)
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
                      "(verschwindet beim Schließen) · ◆ KI-Vorlage · ♪ Album-Cover · ▭ Kartencover · ⧉ mit Rückseite "
                      "· ⚓ Serien-Anker")
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
        self.tabs.addTab(self._series_tab(), "Serie")
        self.tabs.currentChanged.connect(self._render_preview)
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

        if self.focus == self.FOCUS_ALBUM:
            self.tabs.setCurrentIndex(0)
            self.status_label.setText("Album-Cover ändern: Bild in der Galerie anklicken (oder suchen) und "
                                      "„Als Album-Cover setzen“ wählen. Das Kartencover für den Druck ist davon "
                                      "unabhängig (Tab „Karte“).")
        elif self.archive is None:
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
        self.search_title_edit.returnPressed.connect(self._search_from_enter)
        form.addRow("Titel:", self.search_title_edit)
        self.search_artist_edit = QLineEdit()
        self.search_artist_edit.setPlaceholderText("Interpret / Hörspielreihe (optional)")
        self.search_artist_edit.returnPressed.connect(self._search_from_enter)
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
                                 "(bzw. Deezer/iTunes/Spotify) übernehmen - mit Prüfung alt → neu")
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
        tab_layout.setContentsMargins(8, 8, 14, 8)  # rechts Platz fuer die Scrollleiste
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
        form.addRow("Reihe (klein):", self._with_gear(self.series_edit))
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
        self.orientation_combo.currentIndexChanged.connect(self._update_apply_note)
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
        self.btn_front_pick = QPushButton("Als Vorderseite")
        self.btn_front_pick.setObjectName("ghostButton")
        self.btn_front_pick.setToolTip("Das angeklickte Bild (bzw. seine Rückseite, wenn „Rückseite“ gewählt ist) "
                                       "als Motiv der Vorderseite")
        self.btn_front_pick.clicked.connect(lambda: self._pick_source(back=False))
        self.btn_front_reset = QPushButton("Zurücksetzen")
        self.btn_front_reset.setObjectName("ghostButton")
        self.btn_front_reset.setToolTip("Festlegung aufheben – dann gilt wieder das angeklickte Bild")
        self.btn_front_reset.clicked.connect(lambda: self._set_front_source(None))
        for button in (self.btn_front_pick, self.btn_front_reset):
            front_buttons.addWidget(button)
        front_buttons.addStretch(1)
        form.addRow("", front_buttons)
        self.front_extend_check = QCheckBox("Rand per KI ergänzen (1 Bildaufruf)")
        self.front_extend_check.setToolTip("Das Vorderseiten-Motiv vom Bildmodell aufs Kartenformat erweitern – "
                                           "das Ergebnis erscheint als neue Version unter „Neu“")
        self.front_extend_check.toggled.connect(self._update_apply_note)
        form.addRow("", self.front_extend_check)

        back_title = QLabel("Rückseite")
        back_title.setObjectName("sectionTitle")
        form.addRow(back_title)
        self.back_mode_combo = QComboBox()
        self.back_mode_combo.addItem("Keine Rückseite", BACK_MODE_NONE)
        self.back_mode_combo.addItem("Original-Rückseite / gewähltes Bild", BACK_MODE_IMAGE)
        self.back_mode_combo.addItem("KI-Rückseite passend zur Vorderseite (optional mit Wunsch)", BACK_MODE_AI)
        self.back_mode_combo.addItem("Ambient Blur der Vorderseite (ohne KI)", BACK_MODE_BLUR)
        self.back_mode_combo.addItem("Farbverlauf aus den Coverfarben (ohne KI)", BACK_MODE_GRADIENT)
        self.back_mode_combo.currentIndexChanged.connect(self._on_back_mode_changed)
        form.addRow("Motiv:", self.back_mode_combo)
        self.back_tracklist_check = QCheckBox("Trackliste auf Bild (Überschrift + Titel per Code)")
        self.back_tracklist_check.toggled.connect(self._on_back_mode_changed)
        form.addRow("", self.back_tracklist_check)
        self.back_wish_edit = QLineEdit()
        self.back_wish_edit.setPlaceholderText("optional, z.B. „Abraxas auf dem Hexenhaus im Mondlicht“ – leer = automatisch")
        wish_row = QHBoxLayout()
        wish_row.addWidget(self.back_wish_edit, 1)
        self.btn_back_ideas = QPushButton("Idee vorschlagen")
        self.btn_back_ideas.setObjectName("ghostButton")
        self.btn_back_ideas.setToolTip("Das Textmodell schlägt drei Rückseiten-Szenen vor (günstiger Aufruf)")
        self.btn_back_ideas.clicked.connect(self._suggest_back_ideas)
        wish_row.addWidget(self.btn_back_ideas)
        self.back_wish_row = QWidget()
        self.back_wish_row.setLayout(wish_row)
        form.addRow("Wunsch:", self.back_wish_row)
        self.back_wish_label = form.labelForField(self.back_wish_row)
        self.back_regenerate_check = QCheckBox("Beim Übernehmen neu erzeugen (1 Bildaufruf)")
        self.back_regenerate_check.setToolTip("Sonst wird die zuletzt erzeugte KI-Rückseite wiederverwendet")
        self.back_regenerate_check.toggled.connect(self._update_side_controls)
        form.addRow("", self.back_regenerate_check)
        self.back_source_label = QLabel()
        self.back_source_label.setObjectName("folderMeta")
        self.back_source_label.setWordWrap(True)
        form.addRow("", self.back_source_label)
        back_buttons = QHBoxLayout()
        self.btn_back_pick = QPushButton("Als Rückseite")
        self.btn_back_pick.setObjectName("ghostButton")
        self.btn_back_pick.setToolTip("Das angeklickte Bild (bzw. seine Rückseite) als Rückseiten-Motiv verwenden")
        self.btn_back_pick.clicked.connect(lambda: self._pick_source(back=True))
        self.btn_back_file = QPushButton("Aus Datei …")
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
        self.back_extend_check = QCheckBox("Rand per KI ergänzen (1 Bildaufruf)")
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

        self._apply_meta_to_ui(self._card_meta)
        self._on_back_mode_changed()
        return tab

    def _with_gear(self, widget: QWidget) -> QWidget:
        """Eingabefeld mit Zahnrad oben rechts: Schrift fuer Reihe und Titel"""
        row = QWidget()
        layout = QHBoxLayout(row)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)
        layout.addWidget(widget, 1)
        gear = QPushButton()
        gear.setObjectName("ghostButton")
        gear.setIcon(icon_from_glyph("\ue8b8", color=theme.TEXT))  # Settings
        gear.setFixedSize(28, 28)
        gear.setToolTip("Schrift für Reihe und Titel: Schriftart, Größe, Farbe (Vorschau folgt live)")
        gear.clicked.connect(self._open_text_styles)
        layout.addWidget(gear, 0, Qt.AlignmentFlag.AlignTop)
        self._styles_popup: Optional[TextStylesPopup] = None
        return row

    def _open_text_styles(self):
        """Nicht-modales Popup; jede Aenderung geht sofort in die Vorschau"""
        if self._styles_popup is not None and self._styles_popup.isVisible():
            self._styles_popup.raise_()
            self._styles_popup.activateWindow()
            return
        popup = TextStylesPopup(self.series_style, self.title_style, self)
        popup.styles_changed.connect(self._on_text_styles_changed)
        popup.accepted.connect(self._persist_text_styles)
        popup.finished.connect(lambda _r: setattr(self, "_styles_popup", None))
        self._styles_popup = popup
        self.tabs.setCurrentIndex(1)
        self.side_bar.setCurrentIndex(0)
        popup.show()

    def _on_text_styles_changed(self, series_style: TextStyle, title_style: TextStyle):
        self.series_style, self.title_style = series_style, title_style
        self._render_preview()

    def _persist_text_styles(self):
        settings = QSettings()
        settings.setValue(SETTINGS_TITLE_STYLE, json.dumps(self.title_style.to_dict()))
        settings.setValue(SETTINGS_SERIES_STYLE, json.dumps(self.series_style.to_dict()))

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
        self.series_hint_label = QLabel()
        self.series_hint_label.setObjectName("folderMeta")
        self.series_hint_label.setWordWrap(True)
        self.series_hint_label.setToolTip("Zuordnung und Anker-Bilder im Tab „Serie“")
        layout.addWidget(self.series_hint_label)

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

    def _series_tab(self) -> QWidget:
        tab = QScrollArea()
        tab.setWidgetResizable(True)
        tab.setFrameShape(QScrollArea.Shape.NoFrame)
        tab.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        inner = QWidget()
        tab.setWidget(inner)
        layout = QVBoxLayout(inner)
        layout.setContentsMargins(8, 8, 14, 8)
        intro = QLabel("Folgen einer Hörspielreihe bekommen so Kartencover mit denselben Figuren, demselben "
                       "Illustrationsstil und derselben Farbwelt: Anker-Bilder gehen bei jeder KI-Erzeugung als "
                       "erste Vorlagen mit, die Stilbibel beschreibt den Look in Worten.")
        intro.setObjectName("folderMeta")
        intro.setWordWrap(True)
        layout.addWidget(intro)

        form = QFormLayout()
        combo_row = QHBoxLayout()
        self.series_combo = QComboBox()
        self.series_combo.setToolTip("Serie, zu der dieses Album gehört (erkannt aus MusicBrainz bzw. den Tags, "
                                     "vorhandene Profile oder neu anlegen)")
        self.series_combo.currentIndexChanged.connect(self._on_series_combo_changed)
        combo_row.addWidget(self.series_combo, 1)
        self.btn_series_new = QPushButton("Neue Serie …")
        self.btn_series_new.setObjectName("ghostButton")
        self.btn_series_new.clicked.connect(self._create_series)
        combo_row.addWidget(self.btn_series_new)
        form.addRow("Serie:", combo_row)
        self.series_info_label = QLabel()
        self.series_info_label.setObjectName("folderMeta")
        self.series_info_label.setWordWrap(True)
        form.addRow("", self.series_info_label)
        self.series_style_combo = QComboBox()
        self.series_style_combo.addItem("wie im Tab „KI erzeugen“", "")
        for preset in STYLE_PRESETS:
            self.series_style_combo.addItem(preset, preset)
        self.series_style_combo.setToolTip("Fester Stil für alle Folgen der Serie (auch im Serien-Batch)")
        self.series_style_combo.currentIndexChanged.connect(self._on_series_edited)
        form.addRow("Stil:", self.series_style_combo)
        layout.addLayout(form)

        bible_header = QHBoxLayout()
        bible_header.addWidget(QLabel("Stilbibel (Figuren, Palette, Bildsprache – bearbeitbar):"), 1)
        self.btn_bible_suggest = QPushButton("Vorschlagen")
        self.btn_bible_suggest.setObjectName("ghostButton")
        self.btn_bible_suggest.setToolTip("Das Textmodell beschreibt Figuren, Stil und Farben aus den Anker-Bildern "
                                          "und der Recherche (günstiger Aufruf)")
        self.btn_bible_suggest.clicked.connect(self._suggest_style_bible)
        bible_header.addWidget(self.btn_bible_suggest)
        layout.addLayout(bible_header)
        self.bible_edit = QPlainTextEdit()
        self.bible_edit.setPlaceholderText("z.B. Figuren: Fuchs Finn, rotes Fell, blaue Latzhose …\n"
                                           "Stil: weiche Kinderbuch-Illustration …\nFarben: …\nBildsprache: …")
        self.bible_edit.setMinimumHeight(140)
        self.bible_edit.textChanged.connect(self._on_series_edited)
        layout.addWidget(self.bible_edit, 1)

        anchors_title = QLabel(f"Anker-Bilder (max. {MAX_ANCHORS})")
        anchors_title.setObjectName("sectionTitle")
        layout.addWidget(anchors_title)
        self.anchor_list = QListWidget()
        self.anchor_list.setObjectName("coverGallery")
        self.anchor_list.setViewMode(QListWidget.ViewMode.IconMode)
        self.anchor_list.setIconSize(QSize(self.THUMB, self.THUMB))
        self.anchor_list.setGridSize(QSize(self.THUMB + 22, self.THUMB + 40))
        self.anchor_list.setResizeMode(QListWidget.ResizeMode.Adjust)
        self.anchor_list.setMovement(QListWidget.Movement.Static)
        self.anchor_list.setFixedHeight(self.THUMB + 54)
        self.anchor_list.currentItemChanged.connect(lambda *_: self._update_series_ui())
        layout.addWidget(self.anchor_list)
        anchor_buttons = QHBoxLayout()
        self.btn_anchor_add = QPushButton("Angeklicktes Bild als Anker")
        self.btn_anchor_add.setToolTip("Das gerade groß angezeigte Bild (Vorder- oder Rückseite) als Anker der Serie "
                                       "– es wird dafür im Archiv behalten")
        self.btn_anchor_add.clicked.connect(self._add_anchor_from_preview)
        self.btn_anchor_remove = QPushButton("Anker entfernen")
        self.btn_anchor_remove.setObjectName("ghostButton")
        self.btn_anchor_remove.clicked.connect(self._remove_selected_anchor)
        self.btn_character_sheet = QPushButton("Charakterblatt erzeugen (1 Bildaufruf)")
        self.btn_character_sheet.setObjectName("ghostButton")
        self.btn_character_sheet.setToolTip("Ein Bild mit den Hauptfiguren auf neutralem Hintergrund (Nano Banana, "
                                            "Anker als Vorlage) – wird im Archiv gespeichert und als Anker gesetzt")
        self.btn_character_sheet.clicked.connect(self._generate_character_sheet)
        for button in (self.btn_anchor_add, self.btn_anchor_remove, self.btn_character_sheet):
            anchor_buttons.addWidget(button)
        anchor_buttons.addStretch(1)
        layout.addLayout(anchor_buttons)
        self.series_status = QLabel()
        self.series_status.setObjectName("folderMeta")
        self.series_status.setWordWrap(True)
        layout.addWidget(self.series_status)

        self._series_save_timer = QTimer(self)
        self._series_save_timer.setSingleShot(True)
        self._series_save_timer.setInterval(800)
        self._series_save_timer.timeout.connect(self._save_series_profile)
        self._series_loading = False
        return tab

    # --- Serie --------------------------------------------------------------------------------

    def _series_store(self):
        return self.archive.series if self.archive is not None else None

    def _load_series(self):
        """Profil aus der Album-Zuordnung im Archiv laden und Auswahl aufbauen"""
        store = self._series_store()
        if store is not None:
            record = self.archive.load_album(self.album_id)
            if record is not None and record.series_id:
                self.series_profile = store.load(record.series_id)
        self._rebuild_series_combo()

    def _rebuild_series_combo(self):
        self._series_loading = True
        self.series_combo.blockSignals(True)
        self.series_combo.clear()
        self.series_combo.addItem("keine", "")
        store = self._series_store()
        profiles = store.list_all() if store is not None else []
        for profile in profiles:
            self.series_combo.addItem(profile.name, profile.id)
        detected_name, detected_mb = self._detected_series
        if detected_name and not any(self._matches_detected(p) for p in profiles):
            self.series_combo.addItem(f"Erkannt: {detected_name} (neu anlegen)", "__detected__")
        _select_data(self.series_combo, self.series_profile.id if self.series_profile is not None else "")
        self.series_combo.blockSignals(False)
        self.series_combo.setEnabled(store is not None)
        self.btn_series_new.setEnabled(store is not None)
        self._load_series_editor()
        self._series_loading = False
        self._update_series_ui()

    def _matches_detected(self, profile: SeriesProfile) -> bool:
        from core.series import normalize_series_name
        name, mb_id = self._detected_series
        if mb_id and profile.mb_series_id == mb_id:
            return True
        return bool(name) and normalize_series_name(profile.name) == normalize_series_name(name)

    def _load_series_editor(self):
        """Stilbibel, Stil und Anker des gewaehlten Profils in die Felder"""
        self._series_loading = True
        profile = self.series_profile
        self.bible_edit.setPlainText(profile.style_bible if profile else "")
        _select_data(self.series_style_combo, profile.style if profile and profile.style in STYLE_PRESETS else "")
        self.anchor_list.clear()
        self._anchor_images = []
        if profile is not None and self.archive is not None:
            for anchor in profile.anchors:
                data = load_anchor_bytes(self.archive, anchor)
                if not data:
                    continue
                self._anchor_images.append(data)
                with Image.open(io.BytesIO(data)) as img:
                    img.load()
                    thumb = img.convert("RGB")
                thumb.thumbnail((self.THUMB * 2, self.THUMB * 2))
                side = "Rückseite" if anchor.side == SIDE_BACK else "Vorderseite"
                item = QListWidgetItem(QIcon(pil_to_pixmap(thumb)), f"Anker {self.anchor_list.count() + 1}")
                item.setToolTip(f"{side} · Album {anchor.album_id[:8]} · Cover {anchor.cover_id}")
                item.setData(Qt.ItemDataRole.UserRole, anchor)
                self.anchor_list.addItem(item)
        self._series_loading = False

    def _on_series_combo_changed(self, *_):
        if self._series_loading:
            return
        data = self.series_combo.currentData()
        if data == "__detected__":
            self._create_series(self._detected_series[0], self._detected_series[1])
            return
        store = self._series_store()
        self._assign_series(store.load(data) if store is not None and data else None)

    def _assign_series(self, profile: Optional[SeriesProfile]):
        """Album dem Profil zuordnen (None = keine Serie). Im Archiv wird das
        erst festgehalten, wenn das Album dort angelegt ist (sonst beim naechsten
        Speichern ueber _persist_album)."""
        self._series_save_timer.stop()
        self.series_profile = profile
        if self.archive is not None and self.archive.load_album(self.album_id) is not None:
            self.archive.assign_album(self.album_id, profile.id if profile else "")
        self._load_series_editor()
        self._update_series_ui()
        if profile is not None:
            self._flash(f"Album der Serie „{profile.name}“ zugeordnet.")

    def _create_series(self, name: str = "", mb_series_id: str = ""):
        store = self._series_store()
        if store is None:
            return
        if not isinstance(name, str) or not name:
            work = describe_work(self.info, self.brief)
            from core.series import clean_series_name
            suggestion = self._detected_series[0] or clean_series_name(work.series) or self.info.artist
            name, ok = QInputDialog.getText(self, "Neue Serie", "Name der Hörspielreihe:", text=suggestion)
            if not ok or not name.strip():
                self._rebuild_series_combo()
                return
            if self._detected_series[0] and name.strip().lower() == self._detected_series[0].lower():
                mb_series_id = self._detected_series[1]
        existing = store.find_by_name(name)
        profile = existing or store.create(name, mb_series_id, self.style_combo.currentText())
        self.series_profile = profile
        self._rebuild_series_combo()
        self._assign_series(profile)

    @guarded_slot("Serie zuordnen")
    def _on_series_detected(self, name: str, mb_series_id: str):
        self._detected_series = (name, mb_series_id)
        store = self._series_store()
        if self.series_profile is None and store is not None and name:
            match = store.find_by_mb_id(mb_series_id) or store.find_by_name(name)
            if match is not None:
                self.series_profile = match
                if self.archive.load_album(self.album_id) is not None:
                    self.archive.assign_album(self.album_id, match.id)
        self._rebuild_series_combo()

    def _start_series_detection(self):
        if self._series_detect_worker is not None and self._series_detect_worker.isRunning():
            return
        release_group_id = ""
        if self.folder.tracks and self.info.tags_verified:
            release_group_id = album_identity.read_mb_ids(self.folder.tracks[0].filepath)["release_group_id"]
        self._series_detect_worker = SeriesDetectWorker(self.info, release_group_id)
        self._series_detect_worker.done.connect(self._on_series_detected)
        self._series_detect_worker.start()

    def _on_series_edited(self, *_):
        if not self._series_loading and self.series_profile is not None:
            self._series_save_timer.start()

    @guarded_slot("Serie speichern")
    def _save_series_profile(self):
        store = self._series_store()
        profile = self.series_profile
        if store is None or profile is None:
            return
        profile.style_bible = self.bible_edit.toPlainText().strip()
        profile.style = self.series_style_combo.currentData() or ""
        store.save(profile)
        self._update_series_ui()

    def _effective_style(self) -> str:
        """Fester Stil der Serie (Tab "Serie") hat Vorrang vor dem Stil aus "KI erzeugen\""""
        if self.series_profile is not None and hasattr(self, "series_style_combo"):
            series_style = self.series_style_combo.currentData() or ""
            if series_style:
                return series_style
        return self.style_combo.currentText()

    def _series_request_fields(self) -> dict:
        """Felder fuer GenerationRequest: Name, Stilbibel und Anker-Bilder der Serie"""
        if self.series_profile is None:
            return {}
        return {"series_name": self.series_profile.name, "series_bible": self.bible_edit.toPlainText().strip(),
                "anchor_images": list(self._anchor_images)}

    def _update_series_ui(self):
        if not hasattr(self, "series_hint_label") or not hasattr(self, "anchor_list"):
            return
        profile = self.series_profile
        has_profile = profile is not None
        anchors = len(self._anchor_images)
        for widget in (self.bible_edit, self.series_style_combo, self.btn_bible_suggest):
            widget.setEnabled(has_profile)
        preview = self._preview_source() if hasattr(self, "side_bar") else None
        self.btn_anchor_add.setEnabled(has_profile and preview is not None and self.archive is not None
                                       and anchors < MAX_ANCHORS)
        self.btn_anchor_remove.setEnabled(has_profile and self.anchor_list.currentItem() is not None)
        self.btn_character_sheet.setEnabled(has_profile and self.client is not None and anchors < MAX_ANCHORS)
        if self.archive is None:
            self.series_info_label.setText("Kein Datenordner gewählt – Serienprofile brauchen das Archiv.")
            self.series_hint_label.setText("")
            return
        detected = self._detected_series[0]
        if profile is None:
            self.series_info_label.setText(f"Erkannt: {detected}" if detected
                                           else "Keine Reihe erkannt – bei Bedarf „Neue Serie …“ anlegen.")
            self.series_hint_label.setText("Keine Serie zugeordnet (Tab „Serie“) – ohne Anker-Bilder.")
            self.series_status.setText("")
            return
        source = " · MusicBrainz-Serie" if profile.mb_series_id else ""
        self.series_info_label.setText(f"Zugeordnet: {profile.name}{source} · {anchors} Anker · "
                                       + ("mit" if profile.style_bible.strip() else "ohne") + " Stilbibel")
        fixed_style = self.series_style_combo.currentData() or ""
        self.series_hint_label.setText(
            f"Serie: {profile.name} – {anchors} Anker-Bild(er) werden als Vorlage mitgegeben"
            + (", dazu die Stilbibel" if self.bible_edit.toPlainText().strip() else "")
            + (f". Fester Serien-Stil „{fixed_style}“ hat Vorrang vor dem Stil hier." if fixed_style else "."))
        self.style_combo.setToolTip(f"Für diese Serie gilt der feste Stil „{fixed_style}“ (Tab „Serie“)"
                                    if fixed_style else "")
        if anchors == 0:
            self.series_status.setText("Noch keine Anker: ein gutes Cover der Reihe anklicken und „Angeklicktes Bild "
                                       "als Anker“ wählen, oder ein Charakterblatt erzeugen.")
        elif anchors >= MAX_ANCHORS:
            self.series_status.setText(f"Alle {MAX_ANCHORS} Anker-Plätze belegt.")
        else:
            self.series_status.setText("")

    @guarded_slot("Anker setzen")
    def _add_anchor_from_preview(self):
        profile, source = self.series_profile, self._preview_source()
        if profile is None or source is None or self.archive is None:
            return
        if len(profile.anchors) >= MAX_ANCHORS:
            QMessageBox.information(self, "Anker", f"Höchstens {MAX_ANCHORS} Anker je Serie – zuerst einen entfernen.")
            return
        entry = self._keep(source.candidate)
        if entry is None:
            return
        side = SIDE_BACK if source.back else SIDE_FRONT
        if source.back and not entry.back_file:
            QMessageBox.information(self, "Anker", "Diese Rückseite liegt nicht im Archiv.")
            return
        if not profile.add_anchor(self.album_id, entry.id, side):
            self._flash("Dieses Bild ist bereits ein Anker.")
            return
        self.archive.set_role(self.album_id, entry.id, ROLE_SERIES_ANCHOR, exclusive=False)
        self._series_store().save(profile)
        self._sync_roles()
        self._load_series_editor()
        self._update_series_ui()
        self._update_mark_bar()
        self._flash(f"✓ Anker {len(profile.anchors)} für „{profile.name}“ gesetzt.")

    def _remove_selected_anchor(self):
        item = self.anchor_list.currentItem()
        profile = self.series_profile
        if item is None or profile is None:
            return
        anchor: AnchorRef = item.data(Qt.ItemDataRole.UserRole)
        profile.remove_anchor(anchor.album_id, anchor.cover_id, anchor.side)
        if not any(a.album_id == anchor.album_id and a.cover_id == anchor.cover_id for a in profile.anchors):
            try:
                self.archive.clear_role(anchor.album_id, anchor.cover_id, ROLE_SERIES_ANCHOR)
            except (OSError, ValueError):
                pass
        self._series_store().save(profile)
        self._sync_roles()
        self._load_series_editor()
        self._update_series_ui()
        self._update_mark_bar()

    def _suggest_style_bible(self):
        profile = self.series_profile
        if profile is None:
            return
        if self.client is None:
            QMessageBox.information(self, "Kein API-Key", "Für die Stilbibel bitte zuerst unter "
                                                          "Kartencover → KI & Online-Dienste einen Gemini-API-Key hinterlegen.")
            return
        if self._bible_worker is not None and self._bible_worker.isRunning():
            return
        if not self._anchor_images and not confirm_action(
                self, "Stilbibel ohne Anker", "Es gibt noch keine Anker-Bilder – die Stilbibel würde nur aus der "
                                              "Recherche entstehen. Trotzdem vorschlagen?", default_no=False):
            return
        self.btn_bible_suggest.setEnabled(False)
        self.btn_bible_suggest.setText("Vorschlag …")
        self._bible_worker = StyleBibleWorker(self.client, text_model(), profile.name, list(self._anchor_images),
                                              self.research_edit.toPlainText())
        self._bible_worker.done.connect(self._on_style_bible)
        self._bible_worker.failed.connect(lambda m: QMessageBox.warning(self, "Keine Stilbibel", m))
        self._bible_worker.finished.connect(lambda: (self.btn_bible_suggest.setEnabled(True),
                                                     self.btn_bible_suggest.setText("Vorschlagen")))
        self._bible_worker.start()

    def _on_style_bible(self, text: str):
        current = self.bible_edit.toPlainText().strip()
        if current and current != text and not confirm_action(
                self, "Stilbibel ersetzen", "Die vorhandene Stilbibel durch den Vorschlag ersetzen?", default_no=False):
            return
        self.bible_edit.setPlainText(text)
        self._save_series_profile()
        self._flash("✓ Stilbibel vorgeschlagen – bei Bedarf bearbeiten.")

    def _generate_character_sheet(self):
        profile = self.series_profile
        if profile is None or self.client is None:
            return
        if self._sheet_worker is not None and self._sheet_worker.isRunning():
            return
        style = self._effective_style()
        self.btn_character_sheet.setEnabled(False)
        self.status_label.setText(f"Charakterblatt für „{profile.name}“ wird erzeugt … "
                                  f"(ca. {ESTIMATED_COST_PER_IMAGE:.2f} USD)")
        self._sheet_worker = CharacterSheetWorker(self.client, image_model(), profile.name, style,
                                                  self.bible_edit.toPlainText(), list(self._anchor_images))
        self._sheet_worker.done.connect(self._on_character_sheet)
        self._sheet_worker.failed.connect(lambda m: QMessageBox.warning(self, "Charakterblatt nicht erzeugt", m))
        self._sheet_worker.finished.connect(self._update_series_ui)
        self._sheet_worker.start()

    @guarded_slot("Charakterblatt speichern")
    def _on_character_sheet(self, image: Image.Image):
        """Im Archiv speichern, als Anker setzen und in der Galerie zeigen"""
        profile = self.series_profile
        if profile is None or self.archive is None:
            return
        self._persist_album()
        entry = self.archive.add_cover(self.album_id, image, KIND_AI, style="Charakterblatt", model=image_model(),
                                       prompt=f"Charakterblatt {profile.name}")
        self.archive.set_role(self.album_id, entry.id, ROLE_SERIES_ANCHOR, exclusive=False)
        candidate = Candidate(SECTION_ARCHIVE, entry.label, _pil_bytes(image), KIND_AI, entry=entry,
                              meta={"style": "Charakterblatt", "model": image_model()})
        candidate._image = image
        self._add_candidate(candidate, select=True)
        profile.add_anchor(self.album_id, entry.id, SIDE_FRONT)
        self._series_store().save(profile)
        self._load_series_editor()
        self._update_series_ui()
        self._flash(f"✓ Charakterblatt gespeichert und als Anker {len(profile.anchors)} gesetzt.")

    # --- Album-Zuordnung ----------------------------------------------------------------------

    def _track_paths(self) -> List[str]:
        return [t.filepath for t in self.folder.tracks]

    def _apply_meta_to_ui(self, meta):
        """Gespeicherte Kartengestaltung (card.json) in die Felder uebernehmen"""
        settings = QSettings()
        self.title_style = TextStyle.from_dict(
            meta.title_style if meta and meta.title_style else json.loads(settings.value(SETTINGS_TITLE_STYLE, "{}")))
        self.series_style = TextStyle.from_dict(
            meta.series_style if meta and meta.series_style else json.loads(settings.value(SETTINGS_SERIES_STYLE, "{}")))
        self.title_edit.setPlainText(meta.title if meta else self.folder_name)
        if meta:
            self.series_edit.setText(meta.series)
            _select_data(self.orientation_combo, meta.orientation)
            self.number_check.setChecked(meta.show_number)
            mode = meta.back_mode
            if mode == BACK_MODE_TRACKLIST:  # alter eigener Modus -> Flag + Bild/Blur
                mode = BACK_MODE_IMAGE if card_store.has_back(self._card_dir) else BACK_MODE_BLUR
                self.back_tracklist_check.setChecked(True)
            else:
                self.back_tracklist_check.setChecked(meta.back_tracklist)
            _select_data(self.back_mode_combo, mode)
            self.back_wish_edit.setText(meta.back_prompt)
            self.back_heading_edit.setText(meta.back_heading)
            self.back_lines_edit.setPlainText("\n".join(meta.back_lines))
        self._on_back_mode_changed()

    def _resolve_album(self):
        """Album-ID aus card.json bzw. ID-Tag der Tracks; sonst Treffer im
        Archiv ueber den Titel; sonst neu (wird erst beim ersten Speichern
        dauerhaft angelegt)."""
        tagged_id = album_identity.read_album_id(self._track_paths())
        album_id = (self._card_meta.album_id if self._card_meta else "") or tagged_id or self._album_id_hint
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
        if self.series_profile is not None:
            self.archive.assign_album(self.album_id, self.series_profile.id)
        # In der Bibliothek sind die Tracks die Originaldateien: dort nur mit der
        # beim ersten Import erteilten Erlaubnis taggen (sonst Verknuepfung ueber Pfad)
        if not self._album_tagged and self.folder.tracks and (not self.library_mode or library_id_tags_allowed()):
            self._release_player()
            album_identity.write_album_id(self._track_paths(), self.album_id)
            self._album_tagged = True

    def _release_player(self):
        parent = self.parent()
        if parent is not None and hasattr(parent, "_release_files"):
            parent._release_files()

    # --- Galerie ------------------------------------------------------------------------------

    def _add_candidate(self, candidate: Candidate, select: bool = False):
        # Erst dekodieren, dann aufnehmen: ein kaputtes Bild (abgeschnittenes
        # eingebettetes Cover, halb synchronisierte Archivdatei) wird uebersprungen
        try:
            thumb = candidate.image().copy()
        except (OSError, ValueError) as e:
            log.warning("Bild %s nicht lesbar, uebersprungen: %s", candidate.label, e)
            return
        self.candidates.append(candidate)
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
            if ROLE_SERIES_ANCHOR in candidate.entry.roles:
                marks += "⚓"
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
        for attr in ("front_source", "back_source", "ai_back"):
            source = getattr(self, attr)
            if source is not None and source.candidate is candidate:
                setattr(self, attr, None)
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
        raw_path = card_store.card_paths(self._card_dir)[1]
        if raw_path.exists():
            try:
                back_raw = card_store.back_paths(self._card_dir)[1]
                self._add_candidate(Candidate(SECTION_CURRENT, self._card_art_label(), raw_path.read_bytes(),
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
        # Ein Klick zeigt das Bild nur in der Vorschau. Vorder- und Rueckseite der
        # Karte aendern sich ausschliesslich ueber "Als Vorderseite" / "Als Rueckseite",
        # "Zuruecksetzen", "Aus Datei" oder eine fertige KI-Erzeugung. Solange keine
        # Vorderseite festgelegt ist, gilt das angeklickte Bild als Vorschlag.
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
        anchors = len(self._anchor_images)
        text = f"Vorlagen: {refs} Bild(er) mit ◆ (max. {MAX_REFERENCE_IMAGES - anchors}"
        text += f" neben {anchors} Serien-Anker(n))" if anchors else ")"
        if refs + anchors > MAX_REFERENCE_IMAGES:
            text += " – zu viele, nur die ersten werden verwendet"
        if not refs:
            text = "Keine Vorlagen – Bilder in der Galerie markieren und „Als KI-Vorlage“ wählen."
        self.refs_label.setText(text)
        self._update_series_ui()

    def _keep_marked(self):
        marked = [c for c in self._marked_candidates() if not c.kept]
        kept, errors = 0, []
        for candidate in marked:
            try:
                self._keep(candidate)
                kept += 1
            except Exception as e:  # ein Fehler darf nicht die ganze Anwendung beenden
                log.exception("Bild konnte nicht im Archiv gespeichert werden: %s", candidate.label)
                errors.append(f"{candidate.label}: {e}")
        self._set_all_marks(False)
        self._flash(f"✓ {kept} Bild(er) im Archiv gespeichert.")
        if errors:
            QMessageBox.warning(self, "Behalten fehlgeschlagen", chr(10).join(errors[:5]))

    def _toggle_reference_marked(self):
        marked = self._marked_candidates()
        new_state = not all(c.reference for c in marked)
        for candidate in marked:
            candidate.reference = new_state
        self._refresh_item_texts()
        self._set_all_marks(False)
        self._flash(f"{len(marked)} Bild(er) {'als KI-Vorlage markiert' if new_state else 'keine Vorlage mehr'}.")

    @guarded_slot("Verwerfen")
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
        self.status_label.setStyleSheet(f"color: {theme.GREEN}; font-weight: bold;")
        self.status_label.setText(text)
        # Timer als Kind des Dialogs: wird der Dialog vorher geschlossen und geloescht,
        # verschwindet der Timer mit ihm, statt auf das geloeschte Label zu feuern.
        # Ein zweites _flash verlaengert die Frist.
        timer = getattr(self, "_flash_timer", None)
        if timer is None:
            timer = self._flash_timer = QTimer(self)
            timer.setSingleShot(True)
            timer.timeout.connect(lambda: self.status_label.setStyleSheet(""))
        timer.start(6000)

    # --- Cover-Suche --------------------------------------------------------------------------

    def _selected_sources(self) -> List[str]:
        return [s for s, check in self.source_checks.items() if check.isChecked() and check.isEnabled()]

    def _search_from_enter(self):
        """Enter in den Suchfeldern - waehrend einer laufenden Suche ignoriert
        (der Suchen-Knopf ist dann ohnehin gesperrt)"""
        if self._search_worker is not None and self._search_worker.isRunning():
            return
        self._start_search()

    def _start_search(self):
        title = self.search_title_edit.text().strip()
        if not title:
            self.search_status.setText("Bitte einen Album- bzw. Folgentitel eingeben.")
            return
        sources = self._selected_sources()
        QSettings().setValue(SETTINGS_SOURCES, sources)
        # Laufende Suche nicht verwerfen, sondern ausmustern (laeuft ohne
        # Wirkung zu Ende) - ein zerstoerter laufender QThread wuerde die App beenden
        retire_worker(self._search_worker)
        self._search_worker = None
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
        if self.archive is not None:
            self._start_series_detection()
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
        style = self._effective_style()
        request = GenerationRequest(
            info=self.info, brief=self.brief, brief_text=self.research_edit.toPlainText(),
            style=style, extra=self.extra_edit.text(),
            orientation=self.orientation_combo.currentData(), use_reference=bool(refs), reference_images=refs,
            **self._series_request_fields())
        self._generation_meta = {"style": style, "model": image_model(), "prompt": self.extra_edit.text()}
        anchors = len(request.anchors())
        self.status_label.setText(f"Erzeuge {count} Motiv(e) mit {len(refs)} Vorlage(n)"
                                  + (f" und {anchors} Serien-Anker(n)" if anchors else "")
                                  + f" … (ca. {count * ESTIMATED_COST_PER_IMAGE:.2f} USD)")
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
            # meta["kind"] ("ai_back") ist nur ein Marker fuer den Dialog, kein Archivfeld
            meta = {k: v for k, v in candidate.meta.items() if k != "kind"}
            entry = self.archive.add_cover(self.album_id, candidate.image(), KIND_AI, **meta)
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
        if any(c.entry is not None and c.entry.id == entry.id for c in self.candidates):
            return  # genau dieses Bild liegt schon im Archiv und in der Galerie
        archived = Candidate(SECTION_ARCHIVE, entry.label, current.data, KIND_LOCAL, entry=entry)
        self._add_candidate(archived)

    @guarded_slot("Album-Cover setzen")
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
        if entry:
            self._persist_album()  # auch bei Archiv-Bildern: Album anlegen und Dateien mit der ID taggen
        source = self.archive.load_image(self.album_id, entry) if entry else candidate.image()
        jpeg = album_cover_jpeg(source)
        self._release_player()
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            updated = sum(1 for t in self.folder.tracks
                          if self.metadata_manager.set_cover_bytes(t.filepath, jpeg, "image/jpeg"))
            if self.library_mode:
                from core.library import COVER_FILE
                cover_file = Path(self.folder.path) / COVER_FILE
                try:
                    cover_file.write_bytes(jpeg)
                    self.folder.cover_path = str(cover_file)
                except OSError as e:
                    log.warning("cover.jpg konnte nicht geschrieben werden: %s", e)
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

    def _card_art_label(self) -> str:
        return "Kartenmotiv (Archiv)" if self.library_mode else "Kartenmotiv (SD)"

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
            title_style=self.title_style.to_dict(),
            series_style=self.series_style.to_dict(),
        )

    @guarded_slot("Kartencover übernehmen")
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
        if entry:
            self._persist_album()
        if entry and not front.back:
            source = self.archive.load_image(self.album_id, entry)
        else:
            source = front.image()
        orientation = self.orientation_combo.currentData()
        art = prepare_card_art(source, orientation)
        QSettings().setValue(ai_settings.SETTINGS_ORIENTATION, orientation)
        QSettings().setValue(ai_settings.SETTINGS_STYLE, self.style_combo.currentText())
        try:
            Path(self._card_dir).mkdir(parents=True, exist_ok=True)
            card_store.save_card(self._card_dir, art, self._card_meta_now(entry), self.folder.index)
        except OSError as e:
            QMessageBox.warning(self, "Speichern fehlgeschlagen", str(e))
            return
        self.folder.card_path = str(card_store.card_paths(self._card_dir)[0])
        role_error = None
        if entry:
            try:
                self.archive.set_role(self.album_id, entry.id, ROLE_CARD_COVER)
            except OSError as e:  # card.png steht schon - die Rolle fehlt nur im Archiv
                role_error = e
            self._sync_roles()
        back_note = self._save_back(front.candidate, orientation)
        self.card_changed = True
        self._card_meta = card_store.load_meta(self._card_dir)
        if current is not candidate:
            # "Aktuell" sofort auf den neuen Stand bringen (inkl. Rueckseite)
            if current is not None:
                self._remove_candidate(current)
            back_raw = card_store.back_paths(self._card_dir)[1]
            self._add_candidate(Candidate(SECTION_CURRENT, self._card_art_label(),
                                          card_store.card_paths(self._card_dir)[1].read_bytes(),
                                          KIND_LOCAL, is_sd_card_art=True,
                                          back_data=back_raw.read_bytes() if back_raw.exists() else None))
        self._render_preview()
        if role_error is not None:
            QMessageBox.warning(self, "Nur teilweise gespeichert",
                                "Das Kartenbild ist gespeichert, im Cover-Archiv konnte es aber nicht als "
                                f"Kartencover vermerkt werden:\n\n{role_error}\n\n"
                                "Bitte „Übernehmen“ noch einmal klicken." + back_note)
        elif self.library_mode:
            self._flash("✓ Kartencover im Archiv gespeichert – wird beim Übertragen auf die SD-Karte "
                        "geschrieben." + back_note)
        else:
            self._flash("✓ Kartencover auf der SD-Karte gespeichert (card.png)." + back_note)

    # --- Rueckseite ---------------------------------------------------------------------------

    def _on_back_mode_changed(self, *_):
        mode = self.back_mode_combo.currentData()
        is_list = self.back_tracklist_check.isChecked() and mode != BACK_MODE_NONE
        self.back_tracklist_check.setVisible(mode != BACK_MODE_NONE)
        for widget in (self.back_heading_edit, self.back_lines_edit, self.btn_back_from_tags):
            widget.setVisible(is_list)
        for widget in (self.btn_back_pick, self.btn_back_file, self.btn_back_reset, self.back_extend_check,
                       self.back_source_label):
            widget.setVisible(mode == BACK_MODE_IMAGE)
        for widget in (self.back_wish_row, self.back_regenerate_check):
            widget.setVisible(mode == BACK_MODE_AI)
        if self.back_wish_label is not None:
            self.back_wish_label.setVisible(mode == BACK_MODE_AI)
        # Eine gewaehlte Rueckseite bleibt gemerkt, auch wenn zwischendurch Blur,
        # Verlauf oder KI eingestellt ist; sie wirkt nur im Modus "Bild".
        if is_list and not self.back_lines_edit.toPlainText().strip():
            self._fill_back_from_tags()
        self._update_side_controls()
        self._render_preview()

    def _fill_back_from_tags(self):
        heading = " · ".join(p for p in (self.series_edit.text().strip(),
                                         self.title_edit.toPlainText().strip().replace("\n", " / ")) if p)
        self.back_heading_edit.setText(heading)
        titles = self.info.titles or [t.display_name for t in self.folder.tracks]
        self.back_lines_edit.setPlainText("\n".join(
            f"{i:02d}. {t.split(' | ')[0]}" for i, t in enumerate(titles, start=1)))

    # --- Bildquellen der beiden Seiten --------------------------------------------------------

    def _browse_source(self) -> Optional[SideSource]:
        """Die gerade angeschaute Seite des angeklickten Bildes (Grundlage fuer
        "Als Vorderseite" / "Als Rueckseite"); None, wenn das Bild keine Rueckseite hat"""
        if self.selected is None:
            return None
        if self.side_bar.currentIndex() == 0:
            return SideSource(self.selected)
        return SideSource(self.selected, back=True) if self.selected.has_back else None

    def _front_candidate(self) -> Optional[Candidate]:
        """Das Cover, dessen Vorderseite die Karte bekommt (festgelegt oder angeklickt)"""
        if self.front_source is not None:
            return self.front_source.candidate
        return self.selected

    def _preview_source(self) -> Optional[SideSource]:
        """Was die Vorschau gerade zeigt: im Reiter "Karte" die Karte selbst
        (festgelegte Seiten), sonst das angeklickte Bild"""
        if self.selected is None:
            return None
        if not self._card_tab_active():
            return self._browse_source()
        if self.side_bar.currentIndex() == 0:
            return self.front_source or SideSource(self.selected)
        if self.back_source is not None and self.back_mode_combo.currentData() == BACK_MODE_IMAGE:
            return self.back_source
        front = self._front_candidate()
        if front is not None and front.has_back:
            return SideSource(front, back=True)
        return None

    def _card_tab_active(self) -> bool:
        return hasattr(self, "tabs") and self.tabs.currentIndex() == 1

    def _front_image(self) -> Optional[Image.Image]:
        source = self.front_source or (SideSource(self.selected) if self.selected else None)
        return source.image() if source else None

    def _back_source_image(self, front: Optional[Candidate]) -> Optional[Image.Image]:
        """Bildquelle fuer die Rueckseite: explizit gewaehlte Seite, sonst die
        Rueckseite des Vorderseiten-Covers, bei Trackliste sonst dessen Vorderseite"""
        mode = self.back_mode_combo.currentData()
        if self.front_source is not None:
            front = self.front_source.candidate
        front_image = (self.front_source.image() if self.front_source is not None
                       else (front.image() if front is not None else None))
        size = CardSpec().size_px(self.orientation_combo.currentData())
        if mode == BACK_MODE_BLUR:
            return blur_background(front_image, size) if front_image is not None else None
        if mode == BACK_MODE_GRADIENT:
            return gradient_background(front_image, size) if front_image is not None else None
        if mode == BACK_MODE_AI:
            source = self._ai_back_source()
            if source is not None:
                return source.image()
            # Platzhalter bis zur Erzeugung beim Uebernehmen
            return blur_background(front_image, size) if front_image is not None else None
        if self.back_source is not None:
            return self.back_source.image()
        if front is None:
            return None
        if front.has_back:
            return front.back_image()
        return None

    def _pick_source(self, back: bool):
        """Die angeschaute Seite des angeklickten Bildes als Vorder- bzw. Rueckseiten-Motiv festlegen"""
        source = self._browse_source()
        if source is None:
            if self.selected is not None:
                self._flash("Dieses Bild hat keine Rückseite – bitte „Vorderseite“ wählen oder ein anderes Bild.")
            return
        if back:
            if self.back_mode_combo.currentData() != BACK_MODE_IMAGE:
                _select_data(self.back_mode_combo, BACK_MODE_IMAGE)  # Wahl soll sichtbar wirken
            self._set_back_source(source)
            self._flash(f"Rückseite: {source.label}")
        else:
            self._set_front_source(source)
            self._flash(f"Vorderseite: {source.label}")

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

    @guarded_slot("Rückseite hinzufügen")
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

    def _front_state_text(self) -> str:
        if self.front_source is not None:
            return self.front_source.label
        if self.selected is not None:
            return f"{self.selected.label} (angeklickt, nicht festgelegt)"
        return "noch keine – Bild anklicken und „Als Vorderseite“"

    def _back_state_text(self) -> str:
        mode = self.back_mode_combo.currentData()
        front = self._front_candidate()
        if mode == BACK_MODE_NONE:
            return "keine"
        if mode == BACK_MODE_AI:
            source = self._ai_back_source()
            if source is not None and not self._needs_ai_back():
                return f"KI-Rückseite {source.candidate.label} (wird wiederverwendet)"
            if source is not None:
                return "KI-Rückseite wird beim Übernehmen neu erzeugt"
            return "KI-Rückseite wird beim Übernehmen erzeugt (Vorschau zeigt bis dahin einen Platzhalter)"
        if mode == BACK_MODE_BLUR:
            return "Blur aus der Vorderseite"
        if mode == BACK_MODE_GRADIENT:
            return "Verlauf aus der Vorderseite"
        if self.back_source is not None:
            return self.back_source.label
        if front is not None and front.has_back:
            return f"Rückseite von {front.label}"
        return "noch keine – Bild anklicken und „Als Rückseite“ oder „Aus Datei …“"

    def _update_side_controls(self):
        front_text, back_text = self._front_state_text(), self._back_state_text()
        self.front_source_label.setText(f"Vorderseiten-Motiv: {front_text}")
        self.back_source_label.setText(f"Rückseiten-Motiv: {back_text}")
        self.sides_label.setText(f"Karte · Vorderseite: {front_text} · Rückseite: {back_text}")
        self.btn_front_reset.setEnabled(self.front_source is not None)
        browse = self._browse_source()
        self.btn_front_pick.setEnabled(browse is not None)
        self.btn_back_pick.setEnabled(browse is not None)
        self.btn_back_reset.setEnabled(self.back_source is not None)
        self.btn_back_file.setEnabled(self.selected is not None)
        self._update_buttons()

    def _update_apply_note(self, *_):
        if not hasattr(self, "btn_apply"):
            return
        jobs, skipped = [], []
        if self.front_extend_check.isChecked():
            if self._extension_fits(back=False):
                skipped.append("Vorderseite")
            else:
                jobs.append("Rand der Vorderseite ergänzen")
        if hasattr(self, "back_tracklist_check") and self._needs_ai_back():
            jobs.append("KI-Rückseite erzeugen")
        if self.back_extend_check.isChecked() and self.back_mode_combo.currentData() == BACK_MODE_IMAGE:
            if self._extension_fits(back=True):
                skipped.append("Rückseite")
            else:
                jobs.append("Rand der Rückseite ergänzen")
        skip_note = (f" {' und '.join(skipped)} hat schon das Kartenformat – Randergänzung entfällt (kein Aufruf)."
                     if skipped else "")
        if jobs:
            self.apply_note.setText(f"Beim Übernehmen per KI: {', '.join(jobs)} "
                                    f"(ca. {len(jobs) * ESTIMATED_COST_PER_IMAGE:.2f} USD), dann speichern."
                                    + skip_note)
        elif skipped:
            self.apply_note.setText("Beim Übernehmen wird direkt gespeichert." + skip_note)
        else:
            self.apply_note.setText("Beim Übernehmen werden Vorder- und Rückseite im Archiv gespeichert und beim "
                                    "Übertragen auf die SD-Karte geschrieben." if self.library_mode else
                                    "Beim Übernehmen werden Vorder- und Rückseite auf die SD-Karte gespeichert.")

    # --- Rand per KI ergaenzen --------------------------------------------------------------

    def _apply(self):
        """Alle Einstellungen uebernehmen: zuerst die angehakten Randergaenzungen
        nacheinander ausfuehren (Ergebnisse als neue Versionen unter "Neu"),
        danach Vorder- und Rueckseite speichern."""
        if self._extend_worker is not None and self._extend_worker.isRunning():
            return
        jobs = []
        if self.front_extend_check.isChecked():
            jobs.append("extend_front")
        if self._needs_ai_back():
            jobs.append("generate_back")
        if self.back_extend_check.isChecked() and self.back_mode_combo.currentData() == BACK_MODE_IMAGE:
            jobs.append("extend_back")
        if jobs and self.client is None:
            QMessageBox.information(self, "Kein API-Key", "Für KI-Rückseite und Randergänzung bitte zuerst unter "
                                                          "Kartencover → KI & Online-Dienste einen Gemini-API-Key hinterlegen.")
            return
        self._extend_jobs = jobs
        self._run_next_extension()

    def _suggest_back_ideas(self):
        if self.client is None:
            QMessageBox.information(self, "Kein API-Key", "Für Vorschläge bitte zuerst unter "
                                                          "Kartencover → KI & Online-Dienste einen Gemini-API-Key hinterlegen.")
            return
        if self._ideas_worker is not None and self._ideas_worker.isRunning():
            return
        request = GenerationRequest(info=self.info, brief=self.brief, brief_text=self.research_edit.toPlainText())
        # Ideen beziehen sich auf die aktuell gewaehlte Vorderseite
        front = self.front_source or (SideSource(self.selected) if self.selected else None)
        front_bytes = _pil_bytes(front.image()) if front is not None and front.image() is not None else None
        self.btn_back_ideas.setEnabled(False)
        self.btn_back_ideas.setText("Ideen …")
        self._ideas_worker = BackIdeasWorker(self.client, text_model(), request, front_bytes)
        self._ideas_worker.done.connect(self._show_back_ideas)
        self._ideas_worker.failed.connect(lambda m: QMessageBox.warning(self, "Keine Vorschläge", m))
        self._ideas_worker.finished.connect(lambda: (self.btn_back_ideas.setEnabled(True),
                                                     self.btn_back_ideas.setText("Idee vorschlagen")))
        self._ideas_worker.start()

    def _show_back_ideas(self, ideas):
        if not ideas:
            QMessageBox.information(self, "Keine Vorschläge", "Das Modell hat keine Ideen geliefert.")
            return
        description = getattr(self._ideas_worker, "front_description", "")
        if description:
            self.status_label.setText(f"Vorderseite laut KI: {description}")
        menu = QMenu(self)
        for idea in ideas:
            action = menu.addAction(idea)
            action.triggered.connect(lambda _c=False, text=idea: self._use_back_idea(text))
        menu.exec(self.btn_back_ideas.mapToGlobal(self.btn_back_ideas.rect().bottomLeft()))

    def _use_back_idea(self, text: str):
        self.back_wish_edit.setText(text)
        if self._ai_back_source() is not None:
            self.back_regenerate_check.setChecked(True)  # neue Idee -> beim Uebernehmen neu erzeugen
        self._update_side_controls()

    def _ai_back_source(self) -> Optional[SideSource]:
        """Die zuletzt erzeugte KI-Rueckseite: explizit gesetzt oder die des angeklickten Bildes"""
        if self.ai_back is not None and self.ai_back.candidate in self.candidates:
            return self.ai_back
        # aus dem Archiv geladene oder zuvor angeklickte KI-Rueckseite als Ersatz
        for source in (self.back_source, SideSource(self.selected, back=True) if self.selected else None):
            if (source is not None and source.back and source.candidate.has_back
                    and source.candidate.meta.get("kind") == "ai_back"):
                return source
        return None

    def _needs_ai_back(self) -> bool:
        """KI-Rueckseite gewaehlt und noch keine erzeugte Version vorhanden (oder Neu-Erzeugen angehakt)"""
        if self.back_mode_combo.currentData() != BACK_MODE_AI:
            return False
        if getattr(self, "back_regenerate_check", None) is not None and self.back_regenerate_check.isChecked():
            return True
        return self._ai_back_source() is None

    def _start_back_generation(self) -> bool:
        front = self.front_source or (SideSource(self.selected) if self.selected else None)
        if front is None:
            return False
        request = GenerationRequest(
            info=self.info, brief=self.brief, brief_text=self.research_edit.toPlainText(),
            style=self._effective_style(), orientation=self.orientation_combo.currentData(),
            **self._series_request_fields())
        self.status_label.setText("KI-Rückseite wird erzeugt (passend zur Vorderseite) …")
        self._back_front = front
        self._back_worker = CoverBackGenerateWorker(self.client, image_model(), request, _pil_bytes(front.image()),
                                                    self.back_wish_edit.text(), self.back_tracklist_check.isChecked())
        self._back_worker.done.connect(self._on_back_generated)
        self._back_worker.failed.connect(self._on_back_failed)
        self._back_worker.finished.connect(self._update_buttons)
        self._back_worker.start()
        self._update_buttons()
        return True

    def _on_back_failed(self, message: str):
        QMessageBox.warning(self, "KI-Rückseite nicht erzeugt", f"{message}\n\nEs wird ohne Rückseite gespeichert.")
        self._run_next_extension()

    def _on_back_generated(self, image: Image.Image):
        """Neue Version unter "Neu": Vorderseite der Quelle + erzeugte Rueckseite"""
        base = self._back_front.candidate
        number = sum(1 for c in self.candidates if c.section == SECTION_NEW) + 1
        meta = {"kind": "ai_back", "style": self.style_combo.currentText(), "model": image_model(),
                "prompt": self.back_wish_edit.text().strip() or "passend zur Vorderseite"}
        candidate = Candidate(SECTION_NEW, f"KI-Rückseite · {base.label} #{number}", self._back_front.data(),
                              base.kind if base.kind != KIND_LOCAL else KIND_FILE, meta=meta,
                              back_data=_pil_bytes(image))
        candidate._image = self._back_front.image()
        self._add_candidate(candidate)
        self.ai_back = SideSource(candidate, back=True)
        self.back_regenerate_check.setChecked(False)
        self._update_side_controls()
        self.side_bar.setCurrentIndex(1)
        self._render_preview()
        self._run_next_extension()

    def _extension_fits(self, back: bool) -> bool:
        """Hat das Motiv der Seite schon das Kartenformat? Dann entfaellt der KI-Aufruf."""
        source = self._extension_source(back)
        if source is None:
            return False
        image = source.image()
        return image is not None and fits_card_format(image, self.orientation_combo.currentData())

    def _extension_source(self, back: bool) -> Optional[SideSource]:
        if back:
            if self.back_mode_combo.currentData() != BACK_MODE_IMAGE:
                return None  # Blur/Verlauf/KI sind schon im Kartenformat
            if self.back_source is not None:
                return self.back_source
            front = self._front_candidate()
            if front is not None and front.has_back:
                return SideSource(front, back=True)
            return None
        return self.front_source or (SideSource(self.selected) if self.selected else None)

    def _run_next_extension(self):
        while self._extend_jobs:
            job = self._extend_jobs.pop(0)
            if job == "generate_back":
                if self._start_back_generation():
                    return
                continue
            back = job == "extend_back"
            source = self._extension_source(back)
            if source is None or source.image() is None:
                continue
            if fits_card_format(source.image(), self.orientation_combo.currentData()):
                side = "Rückseite" if back else "Vorderseite"
                self._flash(f"{side} hat schon das Kartenformat – Randergänzung übersprungen (kein Aufruf).")
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
        meta.back_tracklist = self.back_tracklist_check.isChecked()
        meta.back_prompt = self.back_wish_edit.text().strip()
        meta.back_heading = self.back_heading_edit.text().strip()
        meta.back_lines = [l for l in self.back_lines_edit.toPlainText().splitlines() if l.strip()]
        meta.back_extended = extended
        meta.back_archive_cover_id = (self.back_source.candidate.entry.id if self.back_source is not None
                                      and self.back_source.candidate.entry is not None else "")
        return meta

    def _save_back(self, front: Candidate, orientation: str) -> str:
        """Schreibt (oder entfernt) die Rueckseite passend zur Einstellung; gibt einen Hinweis zurueck"""
        mode = self.back_mode_combo.currentData()
        meta = card_store.load_meta(self._card_dir)
        if meta is None:
            return ""
        if mode == BACK_MODE_NONE:
            card_store.delete_back(self._card_dir, meta)
            return ""
        source = self._back_source_image(front)
        if source is None:
            card_store.delete_back(self._card_dir, meta)
            return " Keine Rückseite gespeichert (kein Rückseiten-Bild vorhanden)."
        extended = bool(self.back_source is not None and self.back_source.candidate.meta.get("style") == "Rand ergänzt")
        if mode == BACK_MODE_AI and self._ai_back_source() is None:
            card_store.delete_back(self._card_dir, meta)
            return " KI-Rückseite wurde nicht erzeugt, daher keine Rückseite gespeichert."
        art = prepare_card_art(source, orientation)
        try:
            card_store.save_back(self._card_dir, art, self._back_meta(meta, extended))
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

    @guarded_slot("Bild hinzufügen")
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
            # Nur im Reiter "Karte" wird die fertige Karte (mit Text) gezeigt,
            # sonst das Bild selbst, damit sich Cover unverfaelscht vergleichen lassen
            self._preview_shows_card = self.tabs.currentIndex() == 1
            if self.side_bar.currentIndex() == 1:
                if self._preview_shows_card:
                    source = self._back_source_image(self.selected)
                    if source is None:
                        self.preview.clear()
                        self.preview.setText("Keine Rückseite – unter „Rückseite“ ein Bild wählen oder einen Modus "
                                             "(Blur, Verlauf, KI) einstellen")
                        return
                    meta = self._back_meta(self._card_meta_now(None), False)
                    card = card_store.render_back_from_meta(prepare_card_art(source, orientation), meta)
                else:
                    card = self.selected.back_image()
                    if card is None:
                        self.preview.clear()
                        self.preview.setText("Dieses Bild hat keine eigene Rückseite")
                        return
            elif self._preview_shows_card:
                art = prepare_card_art(self._front_image(), orientation)
                card = card_store.render_from_meta(art, self._card_meta_now(None), self.folder.index)
            else:
                card = self.selected.image()
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
        extending = any(w is not None and w.isRunning() for w in (self._extend_worker, self._back_worker))
        self.btn_apply.setEnabled(not extending and (self.front_source is not None or selected))
        if hasattr(self, "back_regenerate_check"):
            self.back_regenerate_check.setEnabled(self._ai_back_source() is not None)
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
        settings.setValue("cover_manager/maximized", self.isMaximized())
        if not self.isMaximized():
            settings.setValue("cover_manager/geometry", self.saveGeometry())
        settings.setValue("cover_manager/splitter", self.main_splitter.saveState())
        settings.setValue("cover_manager/left_splitter", self.left_splitter.saveState())
        if self._series_save_timer.isActive():
            self._series_save_timer.stop()
            self._save_series_profile()
        for worker in (self._search_worker, self._research_worker, self._generate_worker, self._extend_worker,
                       self._back_worker, self._ideas_worker, self._series_detect_worker, self._bible_worker,
                       self._sheet_worker):
            if worker is not None and worker.isRunning():
                if hasattr(worker, "cancel"):
                    worker.cancel()
                worker.wait(1000)
            # Haengt er noch (Netzwerk, KI), am Leben halten statt mit dem Dialog zu zerstoeren
            if worker is not None and worker.isRunning():
                retire_worker(worker)
        super().done(result)
