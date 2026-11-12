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
    QApplication, QComboBox, QDialog, QFormLayout, QFrame, QGridLayout, QGroupBox, QHBoxLayout,
    QLabel, QLineEdit, QMessageBox, QPushButton, QSizePolicy, QTreeWidget, QTreeWidgetItem, QVBoxLayout,
    QWidget,
)

from core.espuino_device import (
    ASSIGNABLE_MODES, FILE_MODES, DeviceError, assign_card, card_id_from_uid, ws_url,
)
from core.espuino_transfer import safe_name
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


def album_haystack(album) -> str:
    """Wonach die Suche ein Album findet: Name, Titel, Interpret, Pfad"""
    return " ".join(str(x) for x in (album.name, getattr(album, "title", ""), getattr(album, "artist", ""),
                                     getattr(album, "path", ""))).casefold()


def matches(needle: str, haystack: str) -> bool:
    """Alle Woerter der Suche kommen vor (Reihenfolge egal)"""
    return all(word in haystack for word in needle.casefold().split())


class FolderPicker(QDialog):
    """Ordner auf dem ESPuino oder Album aus der Bibliothek waehlen, mit Suche.
    Ergebnis: chosen_folder und chosen_album (None bei einem ESPuino-Ordner)."""

    GROUP_ROLE = Qt.ItemDataRole.UserRole + 1

    def __init__(self, folders: Iterable[str], albums: Iterable, on_device: Callable[[str], bool],
                 album_folder: Callable, parent=None, notes: Optional[dict] = None):
        super().__init__(parent)
        self.setWindowTitle("Ordner oder Album wählen")
        self.resize(1000, 600)
        self.chosen_folder = ""
        self.chosen_album = None
        notes = notes or {}
        layout = QVBoxLayout(self)
        self.search = QLineEdit()
        self.search.setPlaceholderText("Suchen – Name, Interpret, Serie, Pfad, etwa „bibi pferd“")
        self.search.setClearButtonEnabled(True)
        self.search.textChanged.connect(self._filter)
        layout.addWidget(self.search)
        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["Name", "Details", "Ort"])
        self.tree.setColumnWidth(0, 340)
        self.tree.setColumnWidth(1, 260)
        self.tree.itemDoubleClicked.connect(lambda item, _col: self._accept_item(item))
        self.tree.currentItemChanged.connect(lambda *_: self._update_ok())
        layout.addWidget(self.tree, 1)
        self.device_group = QTreeWidgetItem(["Auf dem ESPuino", ""])
        self.library_group = QTreeWidgetItem(["Bibliothek", ""])
        for group in (self.device_group, self.library_group):
            group.setFlags(group.flags() & ~Qt.ItemFlag.ItemIsSelectable)
            font = group.font(0)
            font.setBold(True)
            group.setFont(0, font)
            self.tree.addTopLevelItem(group)
        for folder in sorted(set(folders), key=str.casefold):
            item = QTreeWidgetItem([folder.strip("/") or folder, notes.get(folder.casefold(), "")])
            item.setData(0, Qt.ItemDataRole.UserRole, ("folder", folder))
            item.setData(0, self.GROUP_ROLE, (folder + " " + notes.get(folder.casefold(), "")).casefold())
            self.device_group.addChild(item)
        for album in sorted(albums, key=lambda a: a.name.casefold()):
            target = album_folder(album)
            details = [getattr(album, "artist", "") or "", f"{len(album.tracks)} Titel"]
            details.append("auf dem ESPuino" if on_device(target) else "wird übertragen")
            item = QTreeWidgetItem([album.name, " · ".join(d for d in details if d), os.path.dirname(album.path)])
            item.setToolTip(0, album.path)
            item.setToolTip(1, album.path)
            item.setData(0, Qt.ItemDataRole.UserRole, ("album", album))
            item.setData(0, self.GROUP_ROLE, album_haystack(album))
            self.library_group.addChild(item)
        self.tree.expandAll()
        buttons = QHBoxLayout()
        self.count_label = QLabel()
        self.count_label.setStyleSheet(f"color: {theme.OVERLAY2};")
        buttons.addWidget(self.count_label, 1)
        cancel = QPushButton("Abbrechen")
        cancel.clicked.connect(self.reject)
        buttons.addWidget(cancel)
        self.btn_ok = QPushButton("Übernehmen")
        self.btn_ok.setObjectName("primaryButton")
        self.btn_ok.setDefault(True)
        self.btn_ok.clicked.connect(lambda: self._accept_item(self.tree.currentItem()))
        buttons.addWidget(self.btn_ok)
        layout.addLayout(buttons)
        self._filter()
        self.search.setFocus()

    def _filter(self, *_):
        needle = self.search.text().strip()
        first = None
        for group in (self.device_group, self.library_group):
            shown = 0
            for i in range(group.childCount()):
                item = group.child(i)
                visible = matches(needle, item.data(0, self.GROUP_ROLE)) if needle else True
                item.setHidden(not visible)
                if visible:
                    shown += 1
                    first = first or item
            group.setText(1, f"{shown} von {group.childCount()}" if needle else str(group.childCount()))
        if needle and first is not None:
            self.tree.setCurrentItem(first)
        self._update_ok()

    def _update_ok(self):
        item = self.tree.currentItem()
        self.btn_ok.setEnabled(item is not None and item.data(0, Qt.ItemDataRole.UserRole) is not None
                               and not item.isHidden())

    def _accept_item(self, item):
        data = item.data(0, Qt.ItemDataRole.UserRole) if item is not None else None
        if not data:
            return
        kind, value = data
        if kind == "album":
            self.chosen_album = value
            self.chosen_folder = ""
        else:
            self.chosen_folder = value
        self.accept()


class CardReaderDialog(QDialog):
    """Nicht modal: bleibt offen, waehrend man Karten auflegt und zuweist"""
    show_card_requested = pyqtSignal(str)  # in der Kartenliste zeigen
    assigned = pyqtSignal(str, str, int, object)  # Karte, Ordner, Abspielmodus, vorgegebenes Album oder None
    # Album fehlt auf dem ESPuino: erst uebertragen, dann zuweisen (Karte, Album, Ordner, Abspielmodus)
    transfer_requested = pyqtSignal(str, object, str, int)

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
        self._device_folders = set()  # Ordner auf dem ESPuino (klein), fuer "fehlt dort"
        self._album = None  # aus der Bibliothek gewaehlt (core.library.LibraryAlbum)
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
        self.path_edit = QLineEdit()
        self.path_edit.setPlaceholderText("über „Wählen …“ – Ordner auf dem ESPuino oder Album aus der Bibliothek")
        self.path_edit.textChanged.connect(self._update_path_hint)
        self.btn_choose = QPushButton("Wählen …")
        self.btn_choose.setToolTip("Ordner auf dem ESPuino oder Album aus der Bibliothek suchen und wählen")
        self.btn_choose.clicked.connect(self.choose)
        path_row = QWidget()
        path_layout = QHBoxLayout(path_row)
        path_layout.setContentsMargins(0, 0, 0, 0)
        path_layout.addWidget(self.path_edit, 1)
        path_layout.addWidget(self.btn_choose)
        form.addRow("Ordner/Album:", path_row)
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
        self.folder_choices = []
        self.albums = []
        self.folder_notes = {}
        self.set_folders(folders)
        box.setEnabled(bool(self.host))
        self.assign_note.setText("" if self.host else "Zum Zuweisen mit dem ESPuino verbinden.")
        return box

    def set_albums(self, albums: Iterable):
        """Alben der Bibliothek zur Auswahl (Objekte mit name, path, tracks, artist)"""
        self.albums = list(albums)

    def set_folders(self, folders: Iterable[str], notes: Optional[dict] = None):
        """Ordner auf dem ESPuino (und die der bekannten Karten); notes: Ordner (klein) -> Kartennamen"""
        self.folder_choices = sorted({f for f in folders if f}, key=str.casefold)
        if notes is not None:
            self.folder_notes = notes

    def choose(self):
        """Auswahlfenster: Ordner auf dem ESPuino oder Album aus der Bibliothek"""
        picker = FolderPicker(self.folder_choices, self.albums, self.on_device, self.album_folder, self,
                              notes=self.folder_notes)
        if picker.exec():
            if picker.chosen_album is not None:
                self.choose_album(picker.chosen_album)
            elif picker.chosen_folder:
                self._album = None
                self._set_path(picker.chosen_folder)
        picker.deleteLater()

    def choose_album(self, album):
        self._album = album
        self._set_path(self.album_folder(album))

    @staticmethod
    def album_folder(album) -> str:
        """Ordner, in den die App ein Album auf dem ESPuino legt"""
        return "/" + safe_name(album.name)

    def _album_for_path(self, path: str):
        """Das gewaehlte Bibliotheksalbum, wenn der Ordner dazu passt"""
        album = self._album
        if album is not None and self.album_folder(album).casefold() == path.casefold():
            return album
        return None

    def on_device(self, folder: str) -> bool:
        return folder.strip("/").casefold() in self._device_folders

    def set_device(self, host: str, folders: Iterable[str], device_folders: Iterable[str] = (),
                   notes: Optional[dict] = None):
        """Nach jedem Einlesen: Verbindung und Ordner uebernehmen (das Fenster kann
        offen gewesen sein, bevor der ESPuino verbunden war)"""
        was_connected = bool(self.host)
        self.host = host
        self._device_folders = {f.strip("/").casefold() for f in device_folders}
        self.set_folders(folders, notes)
        self.assign_box.setEnabled(bool(host))
        if not host:
            self.assign_note.setText("Zum Zuweisen mit dem ESPuino verbinden.")
        elif not was_connected:
            self.assign_note.setText("")
            self.status_label.setText("Verbinde mit dem ESPuino …")
            try:
                self.socket.open(QUrl(ws_url(host)))
            except DeviceError as exc:
                self.status_label.setText(str(exc))
        self._update_path_hint()

    @property
    def card_id(self) -> str:
        return re.sub(r"\s+", "", self.card_edit.text())

    @property
    def play_mode(self) -> int:
        return int(self.mode_combo.currentData())

    @property
    def sd_path(self) -> str:
        path = self.path_edit.text().strip().replace("\\", "/")
        return path if not path or path.startswith("/") else "/" + path

    def _set_path(self, path: str):
        self.path_edit.setText(path)

    def _set_mode(self, mode: int):
        index = self.mode_combo.findData(int(mode)) if mode else -1
        if index >= 0:
            self.mode_combo.setCurrentIndex(index)

    def _local(self, sd_path: str) -> str:
        return os.path.join(self.sd_root, *[p for p in sd_path.split("/") if p])

    def _update_path_hint(self, *_):
        single = self.play_mode in FILE_MODES
        hint = ""
        path = self.sd_path
        album = self._album_for_path(path)
        if album is not None and self.host and not self.on_device(path):
            hint = (f"„{album.name}“ fehlt auf dem ESPuino – „Zuweisen“ überträgt es zuerst "
                    f"({len(album.tracks)} Titel) und weist die Karte danach zu.")
        elif album is not None:
            hint = f"Album „{album.name}“ aus der Bibliothek, liegt auf dem ESPuino."
        elif single:
            hint = "Für diesen Abspielmodus den Pfad einer Datei eintragen, etwa /Ordner/titel.mp3."
        elif path and self.sd_root and not os.path.isdir(self._local(path)):
            hint = "Diesen Ordner gibt es auf der SD-Karte im Kartenleser nicht."
        elif path and self.host and self._device_folders and not self.on_device(path) \
                and path.strip("/").count("/") == 0:
            hint = "Diesen Ordner gibt es auf dem ESPuino nicht."
        self.path_hint.setText(hint)
        self.path_hint.setVisible(bool(hint))
        self._update_assign_button()

    def _update_assign_button(self, *_):
        current = self.info if self.info is not None and self.info.card_id == self.card_id else None
        self.btn_assign.setText("Neu zuweisen" if current is not None and current.assignment else "Zuweisen")
        self.btn_assign.setEnabled(bool(self.host) and self.card_id.isdigit() and bool(self.sd_path))

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
        album = self._album_for_path(path)
        if album is not None and not self.on_device(path):
            self.assign_note.setText(f"Übertrage „{album.name}“ …")
            self.transfer_requested.emit(card_id, album, path, mode)  # Hauptfenster: uebertragen, dann zuweisen
            return True
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
        album = album or (self._preset_album if self._preset_folder and
                          self._preset_folder.strip("/").casefold() == path.strip("/").casefold() else None)
        self.assigned.emit(card_id, path, mode, album)  # Hauptfenster merkt die Karte und liest neu ein
        self.assignment_done(card_id, path)
        return True

    def assignment_done(self, card_id: str, path: str):
        """Nach dem Zuweisen (auch nach dem Uebertragen): Karte neu zeigen"""
        self.show_info(self.lookup(card_id))
        self.assign_note.setText(f"Karte {format_card_id(card_id)} zugewiesen: {path}")
        self.assign_note.setStyleSheet(f"color: {theme.GREEN};")
        if self._preset_folder:
            self._set_path(self._preset_folder)  # gleich die naechste Karte fuer diesen Ordner

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
