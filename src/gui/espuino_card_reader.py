"""
Karte auslesen: Studio hoert am Websocket des ESPuino mit, solange das Fenster
offen ist. Jede aufgelegte Karte zeigt Nummer, Namen, Zuweisung auf diesem
ESPuino, Album und die Kartencover vorn und hinten - praktisch beim Bekleben
vieler Karten, damit kein Cover auf der falschen Karte landet.

Liest nur; geschrieben wird nichts. Der ESPuino spielt die Karte dabei wie
gewohnt ab (das laesst sich von aussen nicht verhindern).

Eigene Datei des Forks (siehe core/branding.py).
"""

import html
import json
from dataclasses import dataclass
from typing import Callable, Optional

from PyQt6.QtCore import QUrl, Qt, pyqtSignal
from PyQt6.QtGui import QPixmap
from PyQt6.QtWebSockets import QWebSocket
from PyQt6.QtWidgets import (
    QDialog, QFrame, QGridLayout, QHBoxLayout, QLabel, QPushButton, QSizePolicy, QVBoxLayout, QWidget,
)

from core.espuino_device import DeviceError, ws_url
from core.mediahub_cards import format_card_id
from gui import theme

IMAGE_W, IMAGE_H = 320, 202  # Kartenformat quer


@dataclass
class CardInfo:
    """Was Studio ueber eine Kartennummer weiss (gefuellt vom Hauptfenster)"""
    card_id: str
    known: bool = False
    name: str = ""
    assignment: str = ""  # Ordner/Datei auf diesem ESPuino, leer: nicht zugewiesen
    play_mode: str = ""  # Klartext
    status: str = ""  # z. B. "Ordner fehlt auf der SD-Karte"
    album: str = ""  # Album in der Bibliothek
    album_detail: str = ""  # Titelzahl, Dauer
    front_path: str = ""
    back_path: str = ""
    album_cover_path: str = ""


def _image_box(title: str) -> QWidget:
    box = QWidget()
    lay = QVBoxLayout(box)
    lay.setContentsMargins(0, 0, 0, 0)
    caption = QLabel(title)
    caption.setStyleSheet(f"color: {theme.OVERLAY2}; font-weight: bold;")
    lay.addWidget(caption)
    image = QLabel()
    image.setObjectName("image")
    image.setFixedSize(IMAGE_W, IMAGE_H)
    image.setAlignment(Qt.AlignmentFlag.AlignCenter)
    image.setFrameShape(QFrame.Shape.StyledPanel)
    lay.addWidget(image)
    box.image = image
    return box


def _show_image(label: QLabel, path: str):
    pixmap = QPixmap(path) if path else QPixmap()
    if pixmap.isNull():
        label.setPixmap(QPixmap())
        label.setText("kein Bild")
        return
    label.setText("")
    label.setPixmap(pixmap.scaled(label.size(), Qt.AspectRatioMode.KeepAspectRatio,
                                  Qt.TransformationMode.SmoothTransformation))


class CardReaderDialog(QDialog):
    """Nicht modal: bleibt offen, waehrend man Karten auflegt"""
    show_card_requested = pyqtSignal(str)  # in der Kartenliste zeigen
    assign_requested = pyqtSignal(str)  # unbekannte Karte zuweisen

    def __init__(self, host: str, lookup: Callable[[str], CardInfo], parent=None, listen: bool = True):
        super().__init__(parent)
        self.host = host
        self.lookup = lookup
        self.info: Optional[CardInfo] = None
        self.count = 0
        self.setWindowTitle("Karte auslesen")
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        layout = QVBoxLayout(self)
        self.status_label = QLabel("Verbinde mit dem ESPuino …")
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)
        self.number_label = QLabel()
        self.number_label.setStyleSheet("font-size: 20pt; font-weight: bold;")
        self.number_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(self.number_label)
        self.name_label = QLabel("Karte an den ESPuino halten.")
        self.name_label.setWordWrap(True)
        self.name_label.setStyleSheet("font-size: 14pt;")
        layout.addWidget(self.name_label)

        grid = QGridLayout()
        self.front_box = _image_box("Karte vorne")
        self.back_box = _image_box("Karte hinten")
        self.album_box = _image_box("Album-Cover")
        info_box = QWidget()
        info_layout = QVBoxLayout(info_box)
        info_layout.setContentsMargins(0, 0, 0, 0)
        caption = QLabel("Info")
        caption.setStyleSheet(f"color: {theme.OVERLAY2}; font-weight: bold;")
        info_layout.addWidget(caption)
        self.info_label = QLabel()
        self.info_label.setWordWrap(True)
        self.info_label.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
        self.info_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.info_label.setFixedWidth(IMAGE_W)
        self.info_label.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Expanding)
        info_layout.addWidget(self.info_label, 1)
        grid.addWidget(self.front_box, 0, 0)
        grid.addWidget(self.back_box, 0, 1)
        grid.addWidget(info_box, 1, 0)
        grid.addWidget(self.album_box, 1, 1)
        layout.addLayout(grid)

        buttons = QHBoxLayout()
        self.btn_show = QPushButton("In der Kartenliste zeigen")
        self.btn_show.clicked.connect(lambda: self.info and self.show_card_requested.emit(self.info.card_id))
        buttons.addWidget(self.btn_show)
        self.btn_assign = QPushButton("Diese Karte zuweisen …")
        self.btn_assign.clicked.connect(lambda: self.info and self.assign_requested.emit(self.info.card_id))
        buttons.addWidget(self.btn_assign)
        buttons.addStretch(1)
        close = QPushButton("Schließen")
        close.clicked.connect(self.close)
        buttons.addWidget(close)
        layout.addLayout(buttons)
        self._clear()

        self.socket = QWebSocket()
        self.socket.connected.connect(self._on_connected)
        self.socket.disconnected.connect(self._on_disconnected)
        self.socket.textMessageReceived.connect(self.on_message)
        self.finished.connect(lambda _result: self.socket.close())
        if listen:
            try:
                self.socket.open(QUrl(ws_url(host)))
            except DeviceError as exc:
                self.status_label.setText(str(exc))

    def _clear(self):
        self.number_label.setText("")
        self.info_label.setText("")
        for box in (self.front_box, self.back_box, self.album_box):
            box.image.setPixmap(QPixmap())
            box.image.setText("")
        self.btn_show.setEnabled(False)
        self.btn_assign.setEnabled(False)

    def _on_connected(self):
        self.status_label.setText("Bereit: Karte an den ESPuino halten. Das Fenster liest weiter, bis du es "
                                  "schließt. (Der ESPuino spielt die Karte dabei ab.)")
        self.status_label.setStyleSheet(f"color: {theme.GREEN};")

    def _on_disconnected(self):
        if not self.isVisible():
            return
        self.status_label.setText("Keine Verbindung zum ESPuino – ist er eingeschaltet und im WLAN? "
                                  "Fenster schließen und erneut öffnen, um es noch einmal zu versuchen.")
        self.status_label.setStyleSheet(f"color: {theme.YELLOW};")

    def on_message(self, text: str):
        try:
            message = json.loads(text)
        except ValueError:
            return
        card_id = str(message.get("rfidId") or "").strip() if isinstance(message, dict) else ""
        if card_id.isdigit():
            self.show_info(self.lookup(card_id))

    def show_info(self, info: CardInfo):
        self.info = info
        self.count += 1
        self.number_label.setText(f"Karte {format_card_id(info.card_id)}")
        if info.known:
            self.name_label.setText(info.name)
            self.name_label.setStyleSheet("font-size: 14pt;")
        else:
            self.name_label.setText("Unbekannte Karte – Studio kennt sie noch nicht.")
            self.name_label.setStyleSheet(f"font-size: 14pt; color: {theme.YELLOW};")
        lines = []
        if info.assignment:
            lines.append(f"<b>Auf diesem ESPuino:</b> {_esc(info.assignment)}")
        else:
            lines.append("<b>Auf diesem ESPuino:</b> nicht zugewiesen")
        if info.play_mode:
            lines.append(f"<b>Abspielmodus:</b> {_esc(info.play_mode)}")
        if info.album:
            lines.append(f"<b>Album:</b> {_esc(info.album)}")
        if info.album_detail:
            lines.append(_esc(info.album_detail))
        if info.status:
            lines.append(f"<span style='color:{theme.YELLOW}'>{_esc(info.status)}</span>")
        self.info_label.setText("<br>".join(lines))
        _show_image(self.front_box.image, info.front_path)
        _show_image(self.back_box.image, info.back_path)
        _show_image(self.album_box.image, info.album_cover_path)
        self.btn_show.setEnabled(info.known)
        self.btn_assign.setEnabled(not info.assignment)


def _esc(text: str) -> str:
    return html.escape(text)
