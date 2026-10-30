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

from PyQt6.QtCore import QMimeData, QPoint, QRect, QRectF, QUrl, Qt, pyqtSignal
from PyQt6.QtGui import QColor, QPainter, QPen, QPixmap
from PyQt6.QtWidgets import QListWidget, QWidget

from gui import theme

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


class DropHighlight(QWidget):
    """Hervorhebung eines Drop-Ziels waehrend eines Drags: halbtransparente
    Flaeche mit gestricheltem Rahmen und einem Hinweis, was beim Loslassen
    passiert. Liegt ueber dem Ziel und laesst Maus- und Drag-Ereignisse durch."""

    def __init__(self, parent: QWidget):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.hint = ""
        self.blocked = False  # rot statt blau: Ablegen hier nicht moeglich
        self.hide()

    def show_at(self, rect: QRect, hint: str, blocked: bool = False):
        if self.isVisible() and rect == self.geometry() and (hint, blocked) == (self.hint, self.blocked):
            return
        self.hint, self.blocked = hint, blocked
        self.setGeometry(rect)
        self.raise_()  # ueber den Item-Widgets der Liste
        self.show()
        self.update()

    def paintEvent(self, event):
        color = QColor(theme.RED if self.blocked else theme.BLUE)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        fill = QColor(color)
        fill.setAlpha(45)
        pen = QPen(color, 2, Qt.PenStyle.DashLine)
        painter.setPen(pen)
        painter.setBrush(fill)
        painter.drawRoundedRect(QRectF(self.rect()).adjusted(1, 1, -1, -1), 8, 8)
        font = painter.font()
        font.setBold(True)
        painter.setFont(font)
        metrics = painter.fontMetrics()
        # Auf einer einzelnen Ordnerzeile wuerde das Schild den Ordnernamen
        # verdecken - dort reicht die Hervorhebung selbst
        if not self.hint or self.height() < metrics.height() * 3:
            return
        text = metrics.elidedText(self.hint, Qt.TextElideMode.ElideRight, max(self.width() - 28, 20))
        width = min(metrics.horizontalAdvance(text) + 20, self.width() - 8)
        height = min(metrics.height() + 10, self.height() - 4)
        # Auf grossen Flaechen unten mittig, damit es keinen Inhalt (Trackliste) verdeckt
        top = (self.height() - height) / 2 if self.height() < height * 8 else self.height() - height - 16
        pill = QRectF((self.width() - width) / 2, top, width, height)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(color)
        painter.drawRoundedRect(pill, height / 2, height / 2)
        painter.setPen(QColor(theme.BASE))
        painter.drawText(pill, Qt.AlignmentFlag.AlignCenter, text)


DRAG_IMAGE_WIDTH = 120


def drag_pixmap(source: QPixmap, count: int = 1) -> QPixmap:
    """Drag-Bild aus einer gerenderten Kachel: verkleinert, leicht transparent,
    bei mehreren Alben mit Anzahl-Abzeichen oben rechts"""
    ratio = source.devicePixelRatio() or 1.0
    scaled = source.scaledToWidth(int(DRAG_IMAGE_WIDTH * ratio), Qt.TransformationMode.SmoothTransformation)
    result = QPixmap(scaled.size())
    result.setDevicePixelRatio(ratio)
    result.fill(Qt.GlobalColor.transparent)
    scaled.setDevicePixelRatio(ratio)
    painter = QPainter(result)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setOpacity(0.85)
    painter.drawPixmap(0, 0, scaled)
    if count > 1:
        painter.setOpacity(1.0)
        size = 26
        badge = QRectF(result.width() / ratio - size - 4, 4, size, size)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(theme.BLUE))
        painter.drawEllipse(badge)
        font = painter.font()
        font.setBold(True)
        painter.setFont(font)
        painter.setPen(QColor(theme.BASE))
        painter.drawText(badge, Qt.AlignmentFlag.AlignCenter, str(count))
    painter.end()
    return result


def _folder_label(identity) -> str:
    return f"Ordner {identity:02d}" if isinstance(identity, int) else f"Ordner {identity}"


class FolderListWidget(QListWidget):
    """Ordnerliste der Sidebar als Drop-Ziel: Ablegen auf einem Ordner liefert
    dessen Kennung (Nummer bzw. Name), auf freier Flaeche None (= neuer Ordner)"""
    albums_dropped = pyqtSignal(list, object)  # Albumpfade, Ordnerkennung oder None
    tracks_dropped = pyqtSignal(list, object)  # Dateipfade, Ordnerkennung oder None

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setAcceptDrops(True)
        self.drop_enabled = False  # erst mit geoeffneter SD-Karte
        self.drop_highlight = DropHighlight(self.viewport())

    def _payload(self, event) -> DropPayload:
        return drop_payload_from_mime(event.mimeData()) if self.drop_enabled else DropPayload()

    def _show_target(self, pos: QPoint, payload: DropPayload):
        """Hebt das Ziel unter pos hervor: den Ordner oder die ganze Liste (neuer Ordner)"""
        item = self.itemAt(pos)
        if item is None:
            count = len(payload.albums) if payload.albums else 1
            hint = "Neuer Ordner" if count == 1 else f"{count} neue Ordner"
            self.drop_highlight.show_at(self.viewport().rect(), hint)
            return
        identity = item.data(Qt.ItemDataRole.UserRole)
        rect = self.visualItemRect(item)
        if not isinstance(identity, int):
            self.drop_highlight.show_at(rect, f"{_folder_label(identity)}: nicht möglich", blocked=True)
        elif payload.albums:
            self.drop_highlight.show_at(rect, f"In {_folder_label(identity)}")
        else:
            self.drop_highlight.show_at(rect, f"An {_folder_label(identity)} anhängen")

    def _waiting_for_card(self, event) -> bool:
        """Album/Tracks gezogen, aber noch keine SD-Karte geoeffnet"""
        return not self.drop_enabled and bool(drop_payload_from_mime(event.mimeData()))

    def dragEnterEvent(self, event):
        if self._waiting_for_card(event):
            event.accept()  # sonst kommen keine dragMove-Ereignisse und kein Hinweis
            self.dragMoveEvent(event)
            return
        self.dragMoveEvent(event)

    def dragMoveEvent(self, event):
        if self._waiting_for_card(event):
            # Ablegen nicht erlaubt, aber sagen warum - bisher passierte schlicht nichts
            event.ignore()
            self.drop_highlight.show_at(self.viewport().rect(), "Zuerst SD-Karte öffnen", blocked=True)
            return
        payload = self._payload(event)
        if payload:
            event.setDropAction(Qt.DropAction.CopyAction)
            event.accept()
            self._show_target(event.position().toPoint(), payload)
        else:
            event.ignore()
            self.drop_highlight.hide()

    def dragLeaveEvent(self, event):
        self.drop_highlight.hide()
        event.accept()

    def dropEvent(self, event):
        self.drop_highlight.hide()
        payload = self._payload(event)
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
        self.drop_hint = "In diesen Ordner"  # setzt das Hauptfenster je nach gezeigtem Ordner
        self.drop_highlight = DropHighlight(self)

    def _payload(self, event) -> DropPayload:
        return drop_payload_from_mime(event.mimeData()) if self.drop_enabled else DropPayload()

    def dragEnterEvent(self, event):
        self.dragMoveEvent(event)

    def dragMoveEvent(self, event):
        if self._payload(event):
            event.setDropAction(Qt.DropAction.CopyAction)
            event.accept()
            self.drop_highlight.show_at(self.rect().adjusted(4, 4, -4, -4), self.drop_hint)
        else:
            event.ignore()
            self.drop_highlight.hide()

    def dragLeaveEvent(self, event):
        self.drop_highlight.hide()
        event.accept()

    def dropEvent(self, event):
        self.drop_highlight.hide()
        payload = self._payload(event)
        if not payload:
            event.ignore()
            return
        event.setDropAction(Qt.DropAction.CopyAction)
        event.accept()
        if payload.tracks:
            self.tracks_dropped.emit(payload.tracks)
        if payload.albums:
            self.albums_dropped.emit(payload.albums)
