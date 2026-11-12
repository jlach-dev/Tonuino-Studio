"""
Karten auslesen und zuweisen (ESPuino direkt), in einem nicht modalen Fenster.

Studio hoert am Websocket des ESPuino mit und fragt - wenn unter Einstellungen
eingeschaltet - den USB-Leser ACR122U ab, solange das Fenster offen ist. Jede
aufgelegte Karte zeigt Nummer, Namen, Zuweisung auf diesem ESPuino, Album und
die Kartencover vorn und hinten - praktisch beim Bekleben vieler Karten.
Darunter laesst sich die Karte gleich einem Ordner auf dem ESPuino zuweisen
(POST /rfid, wie in seiner Weboberflaeche); das Fenster bleibt fuer die
naechste Karte offen.

Der ESPuino spielt eine aufgelegte Karte wie gewohnt ab (das laesst sich von
aussen nicht verhindern); am ACR122U spielt nichts. Dessen UID rechnet
core.espuino_device.card_id_from_uid in die Kartennummer des ESPuino um.

Eigene Datei des Forks (siehe core/branding.py).
"""

import html
import json
import os
import re
from dataclasses import dataclass
from typing import Callable, Iterable, Optional

from PyQt6.QtCore import QUrl, Qt, pyqtSignal
from PyQt6.QtGui import QPixmap
from PyQt6.QtWebSockets import QWebSocket
from PyQt6.QtWidgets import (
    QApplication, QComboBox, QDialog, QFileDialog, QFormLayout, QFrame, QGridLayout, QGroupBox, QHBoxLayout,
    QLabel, QLineEdit, QMessageBox, QPushButton, QSizePolicy, QVBoxLayout, QWidget,
)

from core.espuino_device import (
    ASSIGNABLE_MODES, FILE_MODES, DeviceError, assign_card, card_id_from_uid, sd_path_from_local, ws_url,
)
from core.mediahub_cards import format_card_id, play_mode_label
from gui import theme

IMAGE_W, IMAGE_H = 300, 189  # Kartenformat quer
DEFAULT_MODE = 3  # Hoerbuch (merkt sich die Stelle)


@dataclass
class CardInfo:
    """Was Studio ueber eine Kartennummer weiss (gefuellt vom Hauptfenster)"""
    card_id: str
    known: bool = False
    name: str = ""
    assignment: str = ""  # Ordner/Datei auf diesem ESPuino, leer: nicht zugewiesen
    play_mode: str = ""  # Klartext
    mode: int = 0  # Abspielmodus als Zahl (Zuweisung auf dem Geraet, sonst bekannte Karte)
    known_folder: str = ""  # Ordner laut Kartenverzeichnis
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


def _esc(text: str) -> str:
    return html.escape(text)


class CardReaderDialog(QDialog):
    """Nicht modal: bleibt offen, waehrend man Karten auflegt und zuweist"""
    show_card_requested = pyqtSignal(str)  # in der Kartenliste zeigen
    assigned = pyqtSignal(str, str, int, object)  # Karte, Ordner, Abspielmodus, vorgegebenes Album oder None

    def __init__(self, host: str, lookup: Callable[[str], CardInfo], parent=None, listen: bool = True,
                 folders: Iterable[str] = (), sd_root: str = ""):
        """host: ESPuino zum Mithoeren und Zuweisen ("" = nicht verbunden).
        folders: Ordner auf dem ESPuino fuer die Auswahlliste."""
        super().__init__(parent)
        self.host = host
        self.lookup = lookup
        self.sd_root = sd_root if sd_root and os.path.isdir(sd_root) else ""
        self.info: Optional[CardInfo] = None
        self.count = 0
        self._reader_uid = ""  # Karte, die gerade auf dem ACR122U liegt
        self._reader_state = None
        self._preset_folder = ""
        self._preset_album = None
        self.setWindowTitle("Karten auslesen und zuweisen")
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        layout = QVBoxLayout(self)
        self.status_label = QLabel("Verbinde mit dem ESPuino …" if host else
                                   "ESPuino: nicht verbunden – Karten nur über den ACR122U, Zuweisen erst "
                                   "nach dem Verbinden.")
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)
        self.reader_label = QLabel("ACR122U: wird gesucht …")
        self.reader_label.setWordWrap(True)
        self.reader_label.setStyleSheet(f"color: {theme.OVERLAY2};")
        layout.addWidget(self.reader_label)
        self.number_label = QLabel()
        self.number_label.setStyleSheet("font-size: 20pt; font-weight: bold;")
        self.number_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(self.number_label)
        self.name_label = QLabel("Karte an den ESPuino oder auf den ACR122U legen.")
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

        layout.addWidget(self._build_assign(folders))

        buttons = QHBoxLayout()
        self.btn_show = QPushButton("In der Kartenliste zeigen")
        self.btn_show.clicked.connect(lambda: self.info and self.show_card_requested.emit(self.info.card_id))
        buttons.addWidget(self.btn_show)
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
        if listen and host:
            try:
                self.socket.open(QUrl(ws_url(host)))
            except DeviceError as exc:
                self.status_label.setText(str(exc))

    # --- Zuweisen ---------------------------------------------------------------------

    def _build_assign(self, folders: Iterable[str]) -> QGroupBox:
        box = QGroupBox("Auf diesem ESPuino zuweisen")
        form = QFormLayout(box)
        self.card_edit = QLineEdit()
        self.card_edit.setPlaceholderText("erscheint beim Auflegen – oder von Hand eintragen")
        self.card_edit.textChanged.connect(self._update_assign_button)
        form.addRow("Karte:", self.card_edit)
        self.mode_combo = QComboBox()
        for mode in ASSIGNABLE_MODES:
            self.mode_combo.addItem(play_mode_label(str(mode)), mode)
        self.mode_combo.currentIndexChanged.connect(self._update_path_hint)
        form.addRow("Abspielmodus:", self.mode_combo)
        self.path_combo = QComboBox()
        self.path_combo.setEditable(True)
        self.path_combo.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self.path_combo.lineEdit().setPlaceholderText("Ordner auf dem ESPuino wählen oder eintragen")
        self.path_combo.editTextChanged.connect(self._update_path_hint)
        self.set_folders(folders)
        self.btn_browse = QPushButton("Wählen …")
        self.btn_browse.setObjectName("ghostButton")
        self.btn_browse.setEnabled(bool(self.sd_root))
        self.btn_browse.setToolTip("Auf der SD-Karte im Kartenleser wählen" if self.sd_root
                                   else "Nur mit der SD-Karte im Kartenleser – sonst aus der Liste wählen")
        self.btn_browse.clicked.connect(self._browse)
        path_row = QWidget()
        path_layout = QHBoxLayout(path_row)
        path_layout.setContentsMargins(0, 0, 0, 0)
        path_layout.addWidget(self.path_combo, 1)
        path_layout.addWidget(self.btn_browse)
        form.addRow("Ordner/Datei:", path_row)
        self.path_hint = QLabel()
        self.path_hint.setWordWrap(True)
        self.path_hint.setStyleSheet(f"color: {theme.OVERLAY2};")
        self.path_hint.hide()
        form.addRow("", self.path_hint)
        row = QHBoxLayout()
        self.assign_note = QLabel()
        self.assign_note.setWordWrap(True)
        self.assign_note.setStyleSheet(f"color: {theme.OVERLAY2};")
        row.addWidget(self.assign_note, 1)
        self.btn_assign = QPushButton("Zuweisen")
        self.btn_assign.setObjectName("primaryButton")
        self.btn_assign.clicked.connect(self.try_assign)
        row.addWidget(self.btn_assign)
        form.addRow(row)
        self.assign_box = box
        box.setEnabled(bool(self.host))
        self.assign_note.setText("" if self.host else "Zum Zuweisen mit dem ESPuino verbinden.")
        return box

    def set_folders(self, folders: Iterable[str]):
        current = self.path_combo.currentText()
        self.path_combo.blockSignals(True)
        self.path_combo.clear()
        for folder in sorted({f for f in folders if f}, key=str.casefold):
            self.path_combo.addItem(folder)
        self.path_combo.setEditText(current)
        self.path_combo.blockSignals(False)

    @property
    def card_id(self) -> str:
        return re.sub(r"\s+", "", self.card_edit.text())

    @property
    def play_mode(self) -> int:
        return int(self.mode_combo.currentData())

    @property
    def sd_path(self) -> str:
        path = self.path_combo.currentText().strip().replace("\\", "/")
        return path if not path or path.startswith("/") else "/" + path

    def _set_path(self, path: str):
        self.path_combo.setEditText(path)

    def _set_mode(self, mode: int):
        index = self.mode_combo.findData(int(mode)) if mode else -1
        if index >= 0:
            self.mode_combo.setCurrentIndex(index)

    def _local(self, sd_path: str) -> str:
        return os.path.join(self.sd_root, *[p for p in sd_path.split("/") if p])

    def _update_path_hint(self, *_):
        single = self.play_mode in FILE_MODES
        self.path_combo.lineEdit().setPlaceholderText("/Ordner/titel.mp3 auf der SD-Karte" if single
                                                      else "Ordner auf dem ESPuino wählen oder eintragen")
        hint = ""
        path = self.sd_path
        if path and self.sd_root:
            local = self._local(path)
            if single and not os.path.isfile(local):
                hint = "Diese Datei gibt es auf der SD-Karte im Kartenleser nicht."
            elif not single and not os.path.isdir(local):
                hint = "Diesen Ordner gibt es auf der SD-Karte im Kartenleser nicht."
        self.path_hint.setText(hint)
        self.path_hint.setVisible(bool(hint))
        self._update_assign_button()

    def _update_assign_button(self, *_):
        current = self.info if self.info is not None and self.info.card_id == self.card_id else None
        self.btn_assign.setText("Neu zuweisen" if current is not None and current.assignment else "Zuweisen")
        self.btn_assign.setEnabled(bool(self.host) and self.card_id.isdigit() and bool(self.sd_path))

    def _browse(self):
        start = self.sd_root
        if self.sd_path and os.path.exists(self._local(self.sd_path)):
            start = self._local(self.sd_path)
        if self.play_mode in FILE_MODES:
            path, _ = QFileDialog.getOpenFileName(self, "Datei auf der SD-Karte wählen", start)
        else:
            path = QFileDialog.getExistingDirectory(self, "Ordner auf der SD-Karte wählen", start)
        if not path:
            return
        try:
            self._set_path(sd_path_from_local(self.sd_root, path))
        except DeviceError as exc:
            QMessageBox.warning(self, "Karte zuweisen", str(exc))

    def preset(self, card_id: str = "", folder: str = "", album=None):
        """Vorbelegen (Ordner aus der Ordnerseite, Karte aus der Kartenliste); der
        Ordner bleibt fuer weitere Karten stehen"""
        if folder:
            self._preset_folder, self._preset_album = folder, album
            self._set_path(folder)
        if card_id:
            self.show_info(self.lookup(card_id))
            if folder:
                self._set_path(folder)
        self.raise_()
        self.activateWindow()

    def try_assign(self) -> bool:
        card_id, path, mode = self.card_id, self.sd_path, self.play_mode
        if not card_id.isdigit():
            QMessageBox.information(self, "Karte zuweisen", "Noch keine Karte: bitte auflegen oder eintragen.")
            return False
        if not path:
            QMessageBox.information(self, "Karte zuweisen", "Bitte einen Ordner (oder eine Datei) wählen.")
            return False
        before = self.lookup(card_id)
        if before.assignment and before.assignment.strip("/").casefold() != path.strip("/").casefold():
            answer = QMessageBox.question(
                self, "Karte ersetzen?", f"Karte {format_card_id(card_id)} spielt auf diesem ESPuino bisher "
                f"„{before.assignment}“. Stattdessen „{path}“ zuweisen?")
            if answer != QMessageBox.StandardButton.Yes:
                return False
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            assign_card(self.host, card_id, path, mode)
        except DeviceError as exc:
            QApplication.restoreOverrideCursor()
            QMessageBox.warning(self, "Karte zuweisen", str(exc))
            return False
        QApplication.restoreOverrideCursor()
        album = self._preset_album if self._preset_folder and \
            self._preset_folder.strip("/").casefold() == path.strip("/").casefold() else None
        self.assigned.emit(card_id, path, mode, album)  # Hauptfenster merkt die Karte und liest neu ein
        self.show_info(self.lookup(card_id))
        self.assign_note.setText(f"Karte {format_card_id(card_id)} zugewiesen: {path}")
        self.assign_note.setStyleSheet(f"color: {theme.GREEN};")
        if self._preset_folder:
            self._set_path(self._preset_folder)  # gleich die naechste Karte fuer diesen Ordner
        return True

    # --- Lesen ------------------------------------------------------------------------

    def _clear(self):
        self.number_label.setText("")
        self.info_label.setText("")
        for box in (self.front_box, self.back_box, self.album_box):
            box.image.setPixmap(QPixmap())
            box.image.setText("")
        self.btn_show.setEnabled(False)
        self._update_assign_button()

    def _on_connected(self):
        self.status_label.setText("ESPuino: bereit. Das Fenster liest weiter, bis du es schließt. "
                                  "(Der ESPuino spielt eine Karte dabei ab, der ACR122U nicht.)")
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
            self.show_info(self.lookup(card_id), "am ESPuino")

    def on_reader_status(self, status):
        """Status des ACR122U (core.rfid.RfidStatus, alle 500 ms vom RfidPoller).
        Eine Karte wird gezeigt, sobald sie neu aufliegt, nicht bei jedem Abfragen."""
        state = (status.reader, bool(status.uid), status.error)
        if state != self._reader_state:
            self._reader_state = state
            if status.reader == "ok":
                text, color = "ACR122U: bereit – Karte auflegen.", theme.GREEN
            elif status.reader == "error":
                text, color = ("ACR122U: nicht nutzbar" + (f" ({status.error})" if status.error else "")
                               + "."), theme.YELLOW
            else:
                text, color = "ACR122U: nicht angeschlossen.", theme.OVERLAY2
            self.reader_label.setText(text)
            self.reader_label.setStyleSheet(f"color: {color};")
        uid = status.uid if status.reader == "ok" else ""
        if uid == self._reader_uid:
            return
        self._reader_uid = uid
        card_id = card_id_from_uid(uid) if uid else ""
        if card_id:
            self.show_info(self.lookup(card_id), "am ACR122U")

    def reader_off(self, reason: str = "aus – unter Einstellungen einschaltbar."):
        """Der ACR122U wird nicht abgefragt"""
        self._reader_state = None
        self.reader_label.setText(f"ACR122U: {reason}")
        self.reader_label.setStyleSheet(f"color: {theme.OVERLAY2};")

    def show_info(self, info: CardInfo, source: str = ""):
        self.info = info
        self.count += 1
        self.number_label.setText(f"Karte {format_card_id(info.card_id)}"
                                  + (f"  <span style='font-size:10pt; font-weight:normal'>{source}</span>"
                                     if source else ""))
        if info.known:
            self.name_label.setText(info.name)
            self.name_label.setStyleSheet("font-size: 14pt;")
        else:
            self.name_label.setText("Unbekannte Karte – Studio kennt sie noch nicht.")
            self.name_label.setStyleSheet(f"font-size: 14pt; color: {theme.YELLOW};")
        lines = [f"<b>Auf diesem ESPuino:</b> {_esc(info.assignment) if info.assignment else 'nicht zugewiesen'}"]
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
        # Zuweisen vorbelegen: vorgegebener Ordner, sonst die bisherige Zuweisung bzw. der bekannte Ordner
        self.card_edit.setText(info.card_id)
        self._set_path(self._preset_folder or info.assignment or info.known_folder)
        self._set_mode(info.mode or DEFAULT_MODE)
        self.assign_note.setText("" if self.host else "Zum Zuweisen mit dem ESPuino verbinden.")
        self.assign_note.setStyleSheet(f"color: {theme.OVERLAY2};")
        self._update_assign_button()
