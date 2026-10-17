"""
Drag & Drop zwischen Bibliothek, Windows-Explorer und SD-Karte

Gezogen werden Albumordner: aus der Bibliotheksansicht mit dem eigenen
MIME-Typ MIME_ALBUM (Pfade, eine Zeile je Album) plus Datei-URLs, aus dem
Explorer als Datei-URLs (nur Ordner zaehlen). Ziele sind die Ordnerliste
in der Sidebar (FolderListWidget) und die Ordneransicht (DropArea).
"""

import os
from dataclasses import dataclass, field
from typing import List, Optional

from PyQt6.QtCore import QMimeData, QUrl, Qt, pyqtSignal
from PyQt6.QtWidgets import QListWidget, QWidget

MIME_ALBUM = "application/x-tonuino-album"
MIME_TRACKS = "application/x-tonuino-tracks"


@dataclass
class DropPayload:
    """Was gezogen wurde: ganze Albumordner und/oder einzelne Audiodateien"""
    albums: List[str] = field(default_factory=list)
    tracks: List[str] = field(default_factory=list)

    def __bool__(self) -> bool:
        return bool(self.albums or self.tracks)


def make_tracks_mime(paths: List[str]) -> QMimeData:
    mime = QMimeData()
    mime.setData(MIME_TRACKS, "\n".join(paths).encode("utf-8"))
    mime.setUrls([QUrl.fromLocalFile(p) for p in paths])
    mime.setText("\n".join(paths))
    return mime


def drop_payload_from_mime(mime: QMimeData) -> DropPayload:
    """Alben (eigener MIME-Typ oder Ordner-URLs) und Tracks (eigener MIME-Typ
    oder einzelne Audiodatei-URLs) aus einem Drag"""
    payload = DropPayload()
    if mime is None:
        return payload
    if mime.hasFormat(MIME_TRACKS):
        text = bytes(mime.data(MIME_TRACKS)).decode("utf-8", "replace")
        payload.tracks = [line for line in text.splitlines() if line.strip()]
        return payload
    if mime.hasFormat(MIME_ALBUM):
        text = bytes(mime.data(MIME_ALBUM)).decode("utf-8", "replace")
        payload.albums = [line for line in text.splitlines() if line.strip()]
        return payload
    if mime.hasUrls():
        from core.library import is_audio_file
        for url in mime.urls():
            if not url.isLocalFile():
                continue
            local = os.path.normpath(url.toLocalFile())
            if os.path.isdir(local) and local not in payload.albums:
                payload.albums.append(local)
            elif os.path.isfile(local) and is_audio_file(os.path.basename(local)) and local not in payload.tracks:
                payload.tracks.append(local)
    return payload


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
    tracks_dropped = pyqtSignal(list, object)  # Dateipfade, Ordnerkennung oder None

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setAcceptDrops(True)
        self.drop_enabled = False  # erst mit geoeffneter SD-Karte

    def _accepts(self, event) -> bool:
        return self.drop_enabled and bool(drop_payload_from_mime(event.mimeData()))

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
        payload = drop_payload_from_mime(event.mimeData()) if self.drop_enabled else DropPayload()
        if not payload:
            event.ignore()
            return
        item = self.itemAt(event.position().toPoint())
        identity = item.data(Qt.ItemDataRole.UserRole) if item is not None else None
        event.setDropAction(Qt.DropAction.CopyAction)
        event.accept()
        if payload.tracks:
            self.tracks_dropped.emit(payload.tracks, identity)
        if payload.albums:
            self.albums_dropped.emit(payload.albums, identity)


class DropArea(QWidget):
    """Flaeche, die Albumordner annimmt (Ordneransicht im Hauptbereich)"""
    albums_dropped = pyqtSignal(list)  # Albumpfade
    tracks_dropped = pyqtSignal(list)  # Dateipfade

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setAcceptDrops(True)
        self.drop_enabled = False

    def _accepts(self, event) -> bool:
        return self.drop_enabled and bool(drop_payload_from_mime(event.mimeData()))

    def dragEnterEvent(self, event):
        if self._accepts(event):
            event.setDropAction(Qt.DropAction.CopyAction)
            event.accept()
        else:
            event.ignore()

    def dragMoveEvent(self, event):
        self.dragEnterEvent(event)

    def dropEvent(self, event):
        payload = drop_payload_from_mime(event.mimeData()) if self.drop_enabled else DropPayload()
        if not payload:
            event.ignore()
            return
        event.setDropAction(Qt.DropAction.CopyAction)
        event.accept()
        if payload.tracks:
            self.tracks_dropped.emit(payload.tracks)
        if payload.albums:
            self.albums_dropped.emit(payload.albums)
