"""
Bibliotheksansicht: Kachelgitter der Alben auf PC und NAS (core/library.py)

Je Kachel: Cover, Titel, Interpret, Trackzahl und Abzeichen ("auf SD als 03",
"Kartencover ✓", "offline", "neu"). Suchfeld und Serienfilter (Phase-2-Profile)
oben. Die Kacheln lassen sich auf die SD-Ordnerliste bzw. die Ordneransicht
ziehen (gui/library_dnd.py).
"""

from typing import Dict, List, Optional

from PyQt6.QtCore import QSize, Qt, pyqtSignal
from PyQt6.QtGui import QPixmap
from PyQt6.QtWidgets import (
    QAbstractItemView, QComboBox, QFrame, QHBoxLayout, QLabel, QLineEdit, QListWidget, QListWidgetItem,
    QPushButton, QVBoxLayout, QWidget,
)

from core.cover_archive import ROLE_CARD_COVER
from core.library import (
    STATE_LABELS, STATE_NEW, STATE_OFFLINE, Library, LibraryAlbum, LibraryIndex, format_duration,
)
from gui import theme
from gui.common import icon_from_glyph
from gui.library_dnd import make_album_mime

TILE_WIDTH = 176
COVER_SIZE = 150
TILE_HEIGHT = COVER_SIZE + 96

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


class AlbumTile(QFrame):
    """Eine Kachel: Cover, Titel, Interpret, Infozeile, Abzeichen"""

    def __init__(self, album: LibraryAlbum, cover: Optional[QPixmap], badges: List[tuple]):
        super().__init__()
        self.setObjectName("libraryTile")
        self.setFixedSize(TILE_WIDTH, TILE_HEIGHT)
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


def _elide(text: str, limit: int) -> str:
    text = text or ""
    return text if len(text) <= limit else text[:limit - 1].rstrip() + "…"


class LibraryView(QWidget):
    rescan_requested = pyqtSignal()
    sources_requested = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
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
        self.search_edit.setMinimumWidth(220)
        self.search_edit.textChanged.connect(self._refresh)
        head.addWidget(self.search_edit)

        self.series_combo = QComboBox()
        self.series_combo.setMinimumWidth(170)
        self.series_combo.setToolTip("Nach Serie filtern (Serienprofile aus dem Cover-Manager)")
        self.series_combo.currentIndexChanged.connect(self._refresh)
        head.addWidget(self.series_combo)

        self.btn_rescan = QPushButton(" Neu einlesen")
        self.btn_rescan.setObjectName("ghostButton")
        self.btn_rescan.setIcon(icon_from_glyph("", color=theme.TEXT))  # Refresh
        self.btn_rescan.setToolTip("Bibliothek-Ordner neu einlesen (nur geänderte Alben werden gelesen)")
        self.btn_rescan.clicked.connect(self.rescan_requested)
        head.addWidget(self.btn_rescan)

        self.btn_sources = QPushButton(" Ordner …")
        self.btn_sources.setObjectName("ghostButton")
        self.btn_sources.setIcon(icon_from_glyph("", color=theme.TEXT))  # Folder
        self.btn_sources.setToolTip("Bibliothek-Ordner (PC, NAS) festlegen")
        self.btn_sources.clicked.connect(self.sources_requested)
        head.addWidget(self.btn_sources)
        layout.addLayout(head)

        self.hint_label = QLabel()
        self.hint_label.setObjectName("folderMeta")
        self.hint_label.setWordWrap(True)
        layout.addWidget(self.hint_label)

        self.grid = LibraryGrid()
        self.grid.setIconSize(QSize(COVER_SIZE, COVER_SIZE))
        layout.addWidget(self.grid, 1)

        self.empty_label = QLabel()
        self.empty_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty_label.setWordWrap(True)
        self.empty_label.setObjectName("folderMeta")
        layout.addWidget(self.empty_label, 1)
        self.empty_label.hide()
        self._rebuild_series_combo()

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
        for album in albums:
            item = QListWidgetItem()
            item.setData(Qt.ItemDataRole.UserRole, album.path)
            item.setSizeHint(QSize(TILE_WIDTH + 4, TILE_HEIGHT + 4))
            item.setToolTip(album.path)
            flags = item.flags() | Qt.ItemFlag.ItemIsDragEnabled
            if album.offline:
                flags &= ~Qt.ItemFlag.ItemIsDragEnabled
            item.setFlags(flags)
            self.grid.addItem(item)
            self.grid.setItemWidget(item, AlbumTile(album, self._pixmap(album), self.badges_for(album)))

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
