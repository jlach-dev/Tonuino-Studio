"""
Drag & Drop zwischen Bibliothek, Windows-Explorer und SD-Karte

Gezogen werden Albumordner: aus der Bibliotheksansicht mit dem eigenen
MIME-Typ MIME_ALBUM (Pfade, eine Zeile je Album) plus Datei-URLs, aus dem
Explorer als Datei-URLs (nur Ordner zaehlen). Ziele sind die Ordnerliste
in der Sidebar (FolderListWidget) und die Ordneransicht (DropArea).
"""

import os
from typing import List, Optional

from PyQt6.QtCore import QMimeData, QUrl, Qt, pyqtSignal
from PyQt6.QtWidgets import QListWidget, QWidget

MIME_ALBUM = "application/x-tonuino-album"


def make_album_mime(paths: List[str]) -> QMimeData:
    mime = QMimeData()
    mime.setData(MIME_ALBUM, "\n".join(paths).encode("utf-8"))
    mime.setUrls([QUrl.fromLocalFile(p) for p in paths])
    mime.setText("\n".join(paths))
    return mime


def album_paths_from_mime(mime: QMimeData) -> List[str]:
    """Albumordner aus einem Drag: eigener MIME-Typ, sonst lokale Ordner-URLs
    (einzelne Audiodateien ergeben ihren Ordner)"""
    if mime is None:
        return []
    if mime.hasFormat(MIME_ALBUM):
        text = bytes(mime.data(MIME_ALBUM)).decode("utf-8", "replace")
        return [line for line in text.splitlines() if line.strip()]
    paths: List[str] = []
    if mime.hasUrls():
        from core.library import is_audio_file
        for url in mime.urls():
            if not url.isLocalFile():
                continue
            local = os.path.normpath(url.toLocalFile())
            if os.path.isdir(local):
                candidate = local
            elif os.path.isfile(local) and is_audio_file(os.path.basename(local)):
                candidate = os.path.dirname(local)
            else:
                continue
            if candidate not in paths:
                paths.append(candidate)
    return paths


class FolderListWidget(QListWidget):
    """Ordnerliste der Sidebar als Drop-Ziel: Ablegen auf einem Ordner liefert
    dessen Kennung (Nummer bzw. Name), auf freier Flaeche None (= neuer Ordner)"""
    albums_dropped = pyqtSignal(list, object)  # Albumpfade, Ordnerkennung oder None

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setAcceptDrops(True)
        self.drop_enabled = False  # erst mit geoeffneter SD-Karte

    def _accepts(self, event) -> bool:
        return self.drop_enabled and bool(album_paths_from_mime(event.mimeData()))

    def dragEnterEvent(self, event):
        if self._accepts(event):
            event.setDropAction(Qt.DropAction.CopyAction)
            event.accept()
        else:
            event.ignore()

    def dragMoveEvent(self, event):
        if self._accepts(event):
            event.setDropAction(Qt.DropAction.CopyAction)
            event.accept()
        else:
            event.ignore()

    def dragLeaveEvent(self, event):
        event.accept()

    def dropEvent(self, event):
        paths = album_paths_from_mime(event.mimeData()) if self.drop_enabled else []
        if not paths:
            event.ignore()
            return
        item = self.itemAt(event.position().toPoint())
        identity = item.data(Qt.ItemDataRole.UserRole) if item is not None else None
        event.setDropAction(Qt.DropAction.CopyAction)
        event.accept()
        self.albums_dropped.emit(paths, identity)


class DropArea(QWidget):
    """Flaeche, die Albumordner annimmt (Ordneransicht im Hauptbereich)"""
    albums_dropped = pyqtSignal(list)  # Albumpfade

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setAcceptDrops(True)
        self.drop_enabled = False

    def _accepts(self, event) -> bool:
        return self.drop_enabled and bool(album_paths_from_mime(event.mimeData()))

    def dragEnterEvent(self, event):
        if self._accepts(event):
            event.setDropAction(Qt.DropAction.CopyAction)
            event.accept()
        else:
            event.ignore()

    def dragMoveEvent(self, event):
        self.dragEnterEvent(event)

    def dropEvent(self, event):
        paths = album_paths_from_mime(event.mimeData()) if self.drop_enabled else []
        if not paths:
            event.ignore()
            return
        event.setDropAction(Qt.DropAction.CopyAction)
        event.accept()
        self.albums_dropped.emit(paths)
