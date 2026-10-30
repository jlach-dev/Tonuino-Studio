"""
Bibliotheksansicht: Kachelgitter der Alben auf PC und NAS (core/library.py)

Je Kachel: Cover, Titel, Interpret, Trackzahl und Abzeichen ("auf SD als 03",
"Kartencover ✓", "offline", "neu"). Suchfeld und Serienfilter (Phase-2-Profile)
oben. Die Kacheln lassen sich auf die SD-Ordnerliste bzw. die Ordneransicht
ziehen (gui/library_dnd.py).
"""

import os
import subprocess
import sys
from typing import Dict, List, Optional

from PyQt6.QtCore import QPoint, QSize, Qt, QUrl, pyqtSignal
from PyQt6.QtGui import QDesktopServices, QDrag, QPixmap
from PyQt6.QtWidgets import (
    QAbstractItemView, QApplication, QComboBox, QFrame, QHBoxLayout, QLabel, QLineEdit, QListWidget,
    QListWidgetItem, QMenu, QPushButton, QStackedWidget, QVBoxLayout, QWidget,
)

from core.cover_archive import ROLE_CARD_COVER
from core.library import (
    STATE_LABELS, STATE_NEW, STATE_OFFLINE, Library, LibraryAlbum, LibraryIndex, LibraryTrack, format_duration, format_time,
)
from gui import theme
from gui.common import icon_from_glyph
from gui.compact_buttons import CompactButtons
from gui.library_dnd import drag_pixmap, make_album_mime, make_tracks_mime
from gui.workers import AlbumTracksWorker, retire_worker

TILE_WIDTH = 176
COVER_SIZE = 150
TILE_HEIGHT = COVER_SIZE + 96  # Mindesthoehe; tatsaechlich so hoch, wie Schrift und Abzeichen brauchen

SERIES_ALL = "__all__"
SERIES_NONE = "__none__"


class LibraryGrid(QListWidget):
    """Kachelgitter, dessen Eintraege als Alben gezogen werden koennen"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("libraryGrid")
        self.setViewMode(QListWidget.ViewMode.IconMode)
        self.setResizeMode(QListWidget.ResizeMode.Adjust)
        self.setMovement(QListWidget.Movement.Static)
        self.setFlow(QListWidget.Flow.LeftToRight)
        self.setWrapping(True)
        self.setSpacing(10)
        self.setUniformItemSizes(True)
        self.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.setDragEnabled(True)
        self.setDragDropMode(QAbstractItemView.DragDropMode.DragOnly)
        self.setDefaultDropAction(Qt.DropAction.CopyAction)
        self.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)

    def mimeTypes(self):
        return ["application/x-tonuino-album", "text/uri-list", "text/plain"]

    def mimeData(self, items):
        paths = [item.data(Qt.ItemDataRole.UserRole) for item in items
                 if item.flags() & Qt.ItemFlag.ItemIsDragEnabled]
        return make_album_mime(paths)

    def startDrag(self, supported_actions):
        """Eigenes Drag-Bild: die Kachel selbst. QListView zeichnet Item-Widgets
        nicht mit, sonst zoege man nur einen leeren grauen Rahmen."""
        items = [i for i in self.selectedItems() if i.flags() & Qt.ItemFlag.ItemIsDragEnabled]
        if not items:
            return
        drag = QDrag(self)
        drag.setMimeData(self.mimeData(items))
        current = self.currentItem() if self.currentItem() in items else items[0]
        tile = self.itemWidget(current)
        if tile is not None:
            pixmap = drag_pixmap(tile.grab(), len(items))
            ratio = pixmap.devicePixelRatio() or 1.0
            drag.setPixmap(pixmap)
            drag.setHotSpot(QPoint(int(pixmap.width() / ratio / 2), int(pixmap.height() / ratio / 2)))
        drag.exec(Qt.DropAction.CopyAction)


class TrackListWidget(QListWidget):
    """Trackliste eines Albums; die markierten Tracks lassen sich auf die SD-Karte ziehen"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("trackList")
        self.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.setDragEnabled(True)
        self.setDragDropMode(QAbstractItemView.DragDropMode.DragOnly)
        self.setDefaultDropAction(Qt.DropAction.CopyAction)

    def mimeTypes(self):
        return ["application/x-tonuino-tracks", "text/uri-list", "text/plain"]

    def mimeData(self, items):
        return make_tracks_mime([item.data(Qt.ItemDataRole.UserRole) for item in items])

    def selected_paths(self) -> List[str]:
        return [item.data(Qt.ItemDataRole.UserRole) for item in self.selectedItems()]


class AlbumPanel(QWidget):
    """Geoeffnetes Album: Kopf, Trackliste (Mehrfachauswahl, Drag-Quelle) und Aktionen"""
    back_requested = pyqtSignal()
    tags_requested = pyqtSignal(object)  # LibraryAlbum
    recognize_requested = pyqtSignal(object, list)  # LibraryAlbum, markierte Dateien (leer = alle)
    organize_requested = pyqtSignal(object, list)  # LibraryAlbum, markierte Dateien (leer = alle)
    cover_requested = pyqtSignal(object)  # LibraryAlbum: Cover-Manager oeffnen

    def __init__(self, parent=None):
        super().__init__(parent)
        self.album: Optional[LibraryAlbum] = None
        self.tracks: List[LibraryTrack] = []
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(12)

        top = QHBoxLayout()
        self.btn_back = QPushButton(" Zurück zur Übersicht")
        self.btn_back.setObjectName("ghostButton")
        self.btn_back.setIcon(icon_from_glyph("", color=theme.TEXT))  # Arrow back
        self.btn_back.clicked.connect(self.back_requested)
        top.addWidget(self.btn_back)
        top.addStretch(1)
        layout.addLayout(top)

        head = QFrame()
        head.setObjectName("heroCard")
        head_layout = QHBoxLayout(head)
        head_layout.setContentsMargins(20, 16, 20, 16)
        head_layout.setSpacing(20)
        self.cover_label = QLabel()
        self.cover_label.setFixedSize(COVER_SIZE, COVER_SIZE)
        self.cover_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        head_layout.addWidget(self.cover_label)
        info = QVBoxLayout()
        info.setSpacing(2)
        self.title_label = QLabel()
        self.title_label.setObjectName("folderTitle")
        self.title_label.setWordWrap(True)
        info.addWidget(self.title_label)
        self.artist_label = QLabel()
        self.artist_label.setObjectName("folderSubtitle")
        info.addWidget(self.artist_label)
        self.meta_label = QLabel()
        self.meta_label.setObjectName("folderMeta")
        self.meta_label.setWordWrap(True)
        info.addWidget(self.meta_label)
        info.addStretch(1)
        actions = QHBoxLayout()
        self.btn_cover = QPushButton("Cover …")
        self.btn_cover.setToolTip("Cover-Manager für dieses Album: Album-Cover suchen oder per KI erzeugen und in "
                                  "die Dateien schreiben, Kartencover fürs Archiv gestalten")
        self.btn_cover.clicked.connect(lambda: self.cover_requested.emit(self.album))
        actions.addWidget(self.btn_cover)
        self.btn_tags = QPushButton("Tags nachtragen …")
        self.btn_tags.setToolTip("Album, Titel und Interpret aus dem Internet (MusicBrainz, Deezer, iTunes, Spotify) "
                                 "übernehmen – mit Vorschau")
        self.btn_tags.clicked.connect(lambda: self.tags_requested.emit(self.album))
        actions.addWidget(self.btn_tags)
        self.btn_recognize = QPushButton("Stücke erkennen …")
        self.btn_recognize.setToolTip("Titel per Audio-Fingerabdruck erkennen (AcoustID) – für unklare Dateien")
        self.btn_recognize.clicked.connect(
            lambda: self.recognize_requested.emit(self.album, self.track_list.selected_paths()))
        actions.addWidget(self.btn_recognize)
        self.btn_organize = QPushButton("Einsortieren …")
        self.btn_organize.setToolTip("Dateien nach ihren Tags in Interpret/Album-Ordner verschieben (mit Vorschau)")
        self.btn_organize.clicked.connect(
            lambda: self.organize_requested.emit(self.album, self.track_list.selected_paths()))
        actions.addWidget(self.btn_organize)
        actions.addStretch(1)
        info.addLayout(actions)
        head_layout.addLayout(info, 1)
        layout.addWidget(head)

        list_head = QHBoxLayout()
        list_title = QLabel("Tracks")
        list_title.setObjectName("sectionTitle")
        list_head.addWidget(list_title)
        self.track_hint = QLabel("Tracks markieren und auf die Ordnerliste links ziehen (neuer Ordner) oder "
                                 "auf einen Ordner (anhängen)")
        self.track_hint.setObjectName("folderMeta")
        self.track_hint.setWordWrap(True)
        list_head.addWidget(self.track_hint, 1)
        self.btn_all = QPushButton("Alle")
        self.btn_all.setObjectName("ghostButton")
        self.btn_all.clicked.connect(lambda: self.track_list.selectAll())
        list_head.addWidget(self.btn_all)
        layout.addLayout(list_head)

        self.track_list = TrackListWidget()
        layout.addWidget(self.track_list, 1)

    def show_album(self, album: LibraryAlbum, cover: Optional[QPixmap], badges: List[tuple]):
        self.album = album
        self.tracks = []
        if cover is not None and not cover.isNull():
            self.cover_label.setPixmap(cover.scaled(COVER_SIZE, COVER_SIZE, Qt.AspectRatioMode.KeepAspectRatio,
                                                    Qt.TransformationMode.SmoothTransformation))
        else:
            self.cover_label.setPixmap(icon_from_glyph("", color=theme.OVERLAY0, size=64).pixmap(64, 64))
        self.title_label.setText(album.name)
        self.artist_label.setText(album.artist or "–")
        parts = [f"{album.track_count} Tracks", format_duration(album.duration), album.path]
        parts += [text for text, _kind in badges]
        self.meta_label.setText(" · ".join(p for p in parts if p))
        self.track_list.clear()
        self.track_list.addItem("Tracks werden gelesen …")
        self.btn_cover.setEnabled(not album.offline)
        self.btn_tags.setEnabled(not album.offline)
        self.btn_recognize.setEnabled(not album.offline)
        self.btn_organize.setEnabled(not album.offline)

    def set_tracks(self, tracks: List[LibraryTrack]):
        self.tracks = tracks
        self.track_list.clear()
        for track in tracks:
            item = QListWidgetItem(track.display + (f"   {format_time(track.duration)}" if track.duration else ""))
            item.setData(Qt.ItemDataRole.UserRole, track.path)
            item.setToolTip(track.path + (f"\n{track.artist}" if track.artist else ""))
            self.track_list.addItem(item)
        if not tracks:
            self.track_list.addItem("Keine Audiodateien gefunden.")


class AlbumTile(QFrame):
    """Eine Kachel: Cover, Titel, Interpret, Infozeile, Abzeichen"""

    def __init__(self, album: LibraryAlbum, cover: Optional[QPixmap], badges: List[tuple]):
        super().__init__()
        self.setObjectName("libraryTile")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 10)
        layout.setSpacing(4)

        cover_label = QLabel()
        cover_label.setObjectName("libraryTileCover")
        cover_label.setFixedSize(COVER_SIZE, COVER_SIZE)
        cover_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        if cover is not None and not cover.isNull():
            cover_label.setPixmap(cover.scaled(COVER_SIZE, COVER_SIZE, Qt.AspectRatioMode.KeepAspectRatio,
                                               Qt.TransformationMode.SmoothTransformation))
        else:
            cover_label.setPixmap(icon_from_glyph("", color=theme.OVERLAY0, size=64).pixmap(64, 64))  # Album
        layout.addWidget(cover_label, 0, Qt.AlignmentFlag.AlignHCenter)

        title = QLabel(_elide(album.name, 24))
        title.setObjectName("libraryTileTitle")
        title.setToolTip(f"{album.name}\n{album.path}")
        layout.addWidget(title)

        artist = QLabel(_elide(album.artist or "–", 28))
        artist.setObjectName("libraryTileArtist")
        layout.addWidget(artist)

        meta_parts = [f"{album.track_count} Track" if album.track_count == 1 else f"{album.track_count} Tracks"]
        duration = format_duration(album.duration)
        if duration:
            meta_parts.append(duration)
        meta = QLabel(" · ".join(meta_parts))
        meta.setObjectName("folderMeta")
        layout.addWidget(meta)

        badge_row = QHBoxLayout()
        badge_row.setSpacing(4)
        for text, kind in badges[:3]:
            badge = QLabel(text)
            badge.setObjectName(f"libraryBadge{kind}")
            badge.setToolTip(text)
            badge_row.addWidget(badge)
        badge_row.addStretch(1)
        layout.addLayout(badge_row)
        if album.offline:
            self.setProperty("offline", True)
        # Hoehe aus dem echten Platzbedarf (Schriftgroessen je System verschieden) -
        # mit fester Hoehe rutschte der Titel unter Windows ins Cover
        self.setFixedSize(TILE_WIDTH, max(TILE_HEIGHT, layout.sizeHint().height()))


def reveal_in_file_manager(path: str):
    """Ordner im Explorer (Windows), Finder (macOS) bzw. Dateimanager zeigen"""
    if sys.platform == "win32":
        subprocess.Popen(["explorer", os.path.normpath(path)])
    elif sys.platform == "darwin":
        subprocess.Popen(["open", path])
    else:
        QDesktopServices.openUrl(QUrl.fromLocalFile(path))


def _elide(text: str, limit: int) -> str:
    text = text or ""
    return text if len(text) <= limit else text[:limit - 1].rstrip() + "…"


class LibraryView(QWidget):
    rescan_requested = pyqtSignal()
    sources_requested = pyqtSignal()
    tags_requested = pyqtSignal(object)  # LibraryAlbum
    recognize_requested = pyqtSignal(object, list)  # LibraryAlbum, markierte Dateien
    organize_requested = pyqtSignal(object, list)  # LibraryAlbum, markierte Dateien
    cover_requested = pyqtSignal(object)  # LibraryAlbum
    embed_covers_requested = pyqtSignal()  # Album-Cover aus dem Archiv nachtragen (alle Alben ohne Cover)
    transfer_requested = pyqtSignal(list)  # Albumpfade: auf die SD-Karte, je Album ein neuer Ordner

    def __init__(self, parent=None):
        super().__init__(parent)
        self.sd_available = False  # SD-Karte geoeffnet (fuer "Auf SD-Karte uebertragen")
        self._tracks_worker: Optional[AlbumTracksWorker] = None
        self.library: Optional[Library] = None
        self.index = LibraryIndex()
        self.sd_map: Dict[str, int] = {}  # Album-ID -> Ordnernummer auf der SD-Karte
        self._series_of: Dict[str, str] = {}  # Album-ID -> Serien-ID (Archiv)
        self._series_names: Dict[str, str] = {}
        self._card_ids: set = set()  # Album-IDs mit Kartencover im Archiv
        self._pixmaps: Dict[str, QPixmap] = {}
        self._setup_ui()

    def _setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(12)

        head = QHBoxLayout()
        head.setSpacing(10)
        title = QLabel("Bibliothek")
        title.setObjectName("sectionTitle")
        head.addWidget(title)
        self.count_label = QLabel()
        self.count_label.setObjectName("folderMeta")
        head.addWidget(self.count_label)
        head.addStretch(1)

        self.search_edit = QLineEdit()
        self.search_edit.setPlaceholderText("Suchen (Titel, Interpret) …")
        self.search_edit.setClearButtonEnabled(True)
        self.search_edit.setMinimumWidth(150)
        self.search_edit.textChanged.connect(self._refresh)
        head.addWidget(self.search_edit)

        self.series_combo = QComboBox()
        self.series_combo.setMinimumWidth(130)
        self.series_combo.setToolTip("Nach Serie filtern (Serienprofile aus dem Cover-Manager)")
        self.series_combo.currentIndexChanged.connect(self._refresh)
        head.addWidget(self.series_combo)

        self.btn_rescan = QPushButton(" Neu einlesen")
        self.btn_rescan.setObjectName("ghostButton")
        self.btn_rescan.setIcon(icon_from_glyph("", color=theme.TEXT))  # Refresh
        self.btn_rescan.setToolTip("Bibliothek-Ordner neu einlesen (nur geänderte Alben werden gelesen)")
        self.btn_rescan.clicked.connect(self.rescan_requested)
        head.addWidget(self.btn_rescan)

        self.btn_embed = QPushButton(" Cover nachtragen …")
        self.btn_embed.setObjectName("ghostButton")
        self.btn_embed.setIcon(icon_from_glyph("\ue3f4", color=theme.TEXT))  # Image
        self.btn_embed.setToolTip("Alben ohne Cover: Album-Cover aus dem Archiv, zur MusicBrainz-Release-ID oder "
                                  "per Online-Suche vorschlagen – mit Prüfung, bevor etwas geschrieben wird")
        self.btn_embed.clicked.connect(self.embed_covers_requested)
        head.addWidget(self.btn_embed)

        self.btn_sources = QPushButton(" Ordner …")
        self.btn_sources.setObjectName("ghostButton")
        self.btn_sources.setIcon(icon_from_glyph("", color=theme.TEXT))  # Folder
        self.btn_sources.setToolTip("Bibliothek-Ordner (PC, NAS) festlegen")
        self.btn_sources.clicked.connect(self.sources_requested)
        head.addWidget(self.btn_sources)
        layout.addLayout(head)
        # Schmale Fenster: die drei Knoepfe nur als Symbol (siehe gui/compact_buttons.py)
        self._compact_head = CompactButtons(self, head, [self.btn_rescan, self.btn_embed, self.btn_sources])

        self.hint_label = QLabel()
        self.hint_label.setObjectName("folderMeta")
        self.hint_label.setWordWrap(True)
        layout.addWidget(self.hint_label)

        self.pages = QStackedWidget()
        overview = QWidget()
        overview_layout = QVBoxLayout(overview)
        overview_layout.setContentsMargins(0, 0, 0, 0)
        self.grid = LibraryGrid()
        self.grid.setIconSize(QSize(COVER_SIZE, COVER_SIZE))
        self.grid.itemDoubleClicked.connect(lambda item: self.open_album(item.data(Qt.ItemDataRole.UserRole)))
        self.grid.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.grid.customContextMenuRequested.connect(self._show_tile_menu)
        overview_layout.addWidget(self.grid, 1)

        self.empty_label = QLabel()
        self.empty_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty_label.setWordWrap(True)
        self.empty_label.setObjectName("folderMeta")
        overview_layout.addWidget(self.empty_label, 1)
        self.empty_label.hide()
        self.pages.addWidget(overview)

        self.album_panel = AlbumPanel()
        self.album_panel.back_requested.connect(self.show_overview)
        self.album_panel.tags_requested.connect(self.tags_requested)
        self.album_panel.recognize_requested.connect(self.recognize_requested)
        self.album_panel.organize_requested.connect(self.organize_requested)
        self.album_panel.cover_requested.connect(self.cover_requested)
        self.pages.addWidget(self.album_panel)
        layout.addWidget(self.pages, 1)
        self._rebuild_series_combo()

    # --- Kontextmenue der Kacheln ---

    def set_sd_available(self, available: bool):
        self.sd_available = available

    def _show_tile_menu(self, pos: QPoint):
        menu = self.tile_menu(pos)
        if menu is not None:
            menu.exec(self.grid.viewport().mapToGlobal(pos))

    def tile_menu(self, pos: QPoint) -> Optional[QMenu]:
        """Rechtsklick auf eine Kachel: die Aktionen des Album-Panels plus Uebertragen,
        Explorer und Pfad. Offline-Alben: nur "Pfad kopieren". Uebertragen gilt fuer
        alle markierten Kacheln, die anderen Aktionen fuer die angeklickte."""
        item = self.grid.itemAt(pos)
        album = self.index.find(item.data(Qt.ItemDataRole.UserRole)) if item is not None else None
        if album is None:
            return None
        if not item.isSelected():  # Rechtsklick ausserhalb der Auswahl: nur diese Kachel
            self.grid.clearSelection()
            item.setSelected(True)
        selected = [self.index.find(i.data(Qt.ItemDataRole.UserRole)) for i in self.grid.selectedItems()]
        transferable = [a.path for a in selected if a is not None and not a.offline]
        online = not album.offline

        menu = QMenu(self)
        menu.setToolTipsVisible(True)

        def add(text, slot, enabled=True, tip=""):
            action = menu.addAction(text)
            action.setEnabled(enabled)
            action.setToolTip(tip)
            action.triggered.connect(slot)
            return action

        add("Öffnen", lambda: self.open_album(album.path), online)
        menu.addSeparator()
        add("Cover …", lambda: self.cover_requested.emit(album), online)
        add("Tags nachtragen …", lambda: self.tags_requested.emit(album), online)
        add("Stücke erkennen …", lambda: self.recognize_requested.emit(album, []), online)
        add("Einsortieren …", lambda: self.organize_requested.emit(album, []), online)
        menu.addSeparator()
        count = len(transferable)
        add("Auf SD-Karte übertragen" + (f" ({count} Alben)" if count > 1 else ""),
            lambda: self.transfer_requested.emit(transferable), self.sd_available and count > 0,
            "" if self.sd_available else "Zuerst eine SD-Karte öffnen")
        add("Im Explorer zeigen", lambda: reveal_in_file_manager(album.path), online)
        add("Pfad kopieren", lambda: QApplication.clipboard().setText(album.path))
        return menu

    # --- Album-Panel ---

    def open_album(self, path: str):
        """Doppelklick auf eine Kachel: Album mit Trackliste zeigen"""
        album = self.index.find(path)
        if album is None:
            return
        self.album_panel.show_album(album, self._pixmap(album), self.badges_for(album))
        self.pages.setCurrentWidget(self.album_panel)
        # Vorigen Lesevorgang nicht verwerfen, sondern ausmustern (laeuft ohne
        # Wirkung zu Ende, ohne den GUI-Thread zu blockieren)
        retire_worker(self._tracks_worker)
        self._tracks_worker = AlbumTracksWorker(album)
        self._tracks_worker.done.connect(self._on_tracks_read)
        self._tracks_worker.start()

    def _on_tracks_read(self, path: str, tracks: list):
        if self.album_panel.album is not None and self.album_panel.album.path == path:
            self.album_panel.set_tracks(tracks)

    def show_overview(self):
        self.pages.setCurrentIndex(0)

    @property
    def open_album_path(self) -> Optional[str]:
        if self.pages.currentWidget() is self.album_panel and self.album_panel.album is not None:
            return self.album_panel.album.path
        return None

    def shutdown(self):
        if self._tracks_worker is not None and self._tracks_worker.isRunning():
            self._tracks_worker.cancel()
            self._tracks_worker.wait(2000)
        retire_worker(self._tracks_worker)
        self._tracks_worker = None

    # --- Daten ---

    def set_library(self, library: Optional[Library], index: Optional[LibraryIndex], archive=None):
        self.library = library
        self.index = index or LibraryIndex()
        self._pixmaps.clear()
        self._series_of, self._series_names, self._card_ids = {}, {}, set()
        if archive is not None:
            try:
                self._series_names = {p.id: p.name for p in archive.series.list_all()}
                ids = {a.album_id for a in self.index.albums if a.album_id}
                for record in archive.all_albums():
                    if record.series_id:
                        self._series_of[record.id] = record.series_id
                    if record.id in ids and any(ROLE_CARD_COVER in e.roles for e in archive.covers(record.id)):
                        self._card_ids.add(record.id)
            except OSError:
                pass
        self._rebuild_series_combo()
        self._refresh()

    def set_sd_map(self, sd_map: Dict[str, int]):
        self.sd_map = dict(sd_map or {})
        self._refresh()

    def set_scanning(self, scanning: bool):
        self.btn_rescan.setEnabled(not scanning)
        if scanning:
            self.hint_label.setText("Bibliothek wird eingelesen …")

    # --- Darstellung ---

    def _rebuild_series_combo(self):
        current = self.series_combo.currentData()
        self.series_combo.blockSignals(True)
        self.series_combo.clear()
        self.series_combo.addItem("Alle Serien", SERIES_ALL)
        used = {sid for aid, sid in self._series_of.items()}
        for sid, name in sorted(self._series_names.items(), key=lambda kv: kv[1].lower()):
            if sid in used:
                self.series_combo.addItem(name, sid)
        self.series_combo.addItem("Ohne Serie", SERIES_NONE)
        pos = self.series_combo.findData(current) if current is not None else -1
        self.series_combo.setCurrentIndex(pos if pos >= 0 else 0)
        self.series_combo.blockSignals(False)

    def _pixmap(self, album: LibraryAlbum) -> Optional[QPixmap]:
        if not album.thumb or self.library is None:
            return None
        if album.thumb not in self._pixmaps:
            path = self.library.thumb_path(album)
            self._pixmaps[album.thumb] = QPixmap(str(path)) if path is not None else QPixmap()
        pixmap = self._pixmaps[album.thumb]
        return pixmap if not pixmap.isNull() else None

    def badges_for(self, album: LibraryAlbum) -> List[tuple]:
        badges = []
        if album.offline or album.state == STATE_OFFLINE:
            badges.append((STATE_LABELS[STATE_OFFLINE], "Offline"))
        if album.album_id and album.album_id in self.sd_map:
            badges.append((f"auf SD als {self.sd_map[album.album_id]:02d}", "Sd"))
        if album.album_id and album.album_id in self._card_ids:
            badges.append(("Kartencover ✓", "Card"))
        if album.state == STATE_NEW and not album.offline:
            badges.append((STATE_LABELS[STATE_NEW], "New"))
        return badges

    def _matches(self, album: LibraryAlbum) -> bool:
        needle = self.search_edit.text().strip().lower()
        if needle and needle not in f"{album.name} {album.artist} {album.path}".lower():
            return False
        series = self.series_combo.currentData()
        if series in (None, SERIES_ALL):
            return True
        assigned = self._series_of.get(album.album_id, "") if album.album_id else ""
        return (not assigned) if series == SERIES_NONE else assigned == series

    def visible_albums(self) -> List[LibraryAlbum]:
        return [a for a in self.index.albums if self._matches(a)]

    def _refresh(self, *_):
        self.grid.clear()
        albums = self.visible_albums()
        tiles = [AlbumTile(album, self._pixmap(album), self.badges_for(album)) for album in albums]
        # Alle Kacheln gleich hoch (das Gitter nutzt einheitliche Groessen): die hoechste zaehlt
        height = max((tile.height() for tile in tiles), default=TILE_HEIGHT)
        for album, tile in zip(albums, tiles):
            tile.setFixedHeight(height)
            item = QListWidgetItem()
            item.setData(Qt.ItemDataRole.UserRole, album.path)
            item.setSizeHint(QSize(TILE_WIDTH + 4, height + 4))
            item.setToolTip(album.path)
            flags = item.flags() | Qt.ItemFlag.ItemIsDragEnabled
            if album.offline:
                flags &= ~Qt.ItemFlag.ItemIsDragEnabled
            item.setFlags(flags)
            self.grid.addItem(item)
            self.grid.setItemWidget(item, tile)

        total = len(self.index.albums)
        self.count_label.setText(f"{len(albums)} von {total} Alben" if len(albums) != total else
                                 (f"{total} Alben" if total != 1 else "1 Album"))
        if self.library is None:
            self.empty_label.setText("Es ist noch kein Datenordner festgelegt – er wird beim ersten Öffnen des "
                                     "Cover-Managers oder über „Ordner …“ gewählt.")
        elif not self.library.sources:
            self.empty_label.setText("Noch keine Bibliothek-Ordner festgelegt.\n\nÜber „Ordner …“ die Ordner mit "
                                     "deinen Hörspielen und deiner Musik (PC, NAS) hinzufügen.")
        elif not total:
            self.empty_label.setText("In den Bibliothek-Ordnern wurden keine Alben gefunden "
                                     "(ein Album ist ein Ordner mit Audiodateien).")
        else:
            self.empty_label.setText("Kein Album passt zu Suche und Filter.")
        empty = not albums
        self.empty_label.setVisible(empty)
        self.grid.setVisible(not empty)
        hints = []
        if self.index.scanned:
            hints.append(f"Stand: {self.index.scanned.replace('T', ' ')[:16]}")
        offline = sum(1 for a in self.index.albums if a.offline)
        if offline:
            hints.append(f"{offline} Album/Alben offline (Quelle nicht erreichbar)")
        if self.index.orphaned:
            names = ", ".join(o.get("title") or o.get("id", "") for o in self.index.orphaned[:3])
            hints.append(f"{len(self.index.orphaned)} verwaiste Archiv-Einträge ohne Bibliotheksordner ({names})")
        if total:
            hints.append("Alben per Drag & Drop auf die Ordnerliste links (neuer Ordner) oder einen Ordner ziehen")
        self.hint_label.setText(" · ".join(hints))
