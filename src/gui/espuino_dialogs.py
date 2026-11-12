"""
Dialoge des ESPuino-Modus: Einrichten (ESPuino direkt im WLAN oder Mediahub),
Karte direkt am ESPuino zuweisen (core/espuino_device.py) und Alben auf seine
SD-Karte uebertragen (core/espuino_transfer.py).

Eigene Datei des Forks (siehe core/branding.py).
"""

import json
import os
import re
from typing import Dict, Optional

from PyQt6.QtCore import QThread, Qt, QUrl, pyqtSignal
from PyQt6.QtWebSockets import QWebSocket
from PyQt6.QtWidgets import (
    QApplication, QButtonGroup, QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFileDialog, QFormLayout, QHBoxLayout,
    QLabel, QLineEdit, QMessageBox, QPushButton, QRadioButton, QVBoxLayout, QWidget,
)

from core.espuino_device import (
    ASSIGNABLE_MODES, FILE_MODES, SOURCE_DEVICE, SOURCE_MEDIAHUB, DeviceError, assign_card, fetch_info,
    firmware_label, normalize_host, sd_path_from_local, ws_url,
)
from core.espuino_transfer import copy_to_sd, plan_album, safe_name, upload_to_device
from core.mediahub_cards import DB_FILENAME, MediahubError, db_file, format_card_id, load_cards, play_mode_label
from gui import theme


def _with_browse(edit: QLineEdit, slot) -> QWidget:
    row = QWidget()
    lay = QHBoxLayout(row)
    lay.setContentsMargins(0, 0, 0, 0)
    lay.addWidget(edit, 1)
    button = QPushButton("Wählen …")
    button.setObjectName("ghostButton")
    button.clicked.connect(slot)
    lay.addWidget(button)
    return row


class EspuinoSetupDialog(QDialog):
    """Woher die Karten kommen: direkt vom ESPuino im WLAN (Adresse und SD-Karte
    auf dem PC) oder aus dem ESPuino-Mediahub (db.json und Mediathek)"""

    def __init__(self, source: str = SOURCE_DEVICE, host: str = "", sd_root: str = "", db_path: str = "",
                 media_root: str = "", parent=None):
        super().__init__(parent)
        self.setWindowTitle("ESPuino einrichten")
        self.setMinimumWidth(640)
        self.card_count = 0
        layout = QVBoxLayout(self)

        self.radio_device = QRadioButton("ESPuino direkt im WLAN")
        self.radio_hub = QRadioButton("ESPuino-Mediahub")
        group = QButtonGroup(self)
        group.addButton(self.radio_device)
        group.addButton(self.radio_hub)
        (self.radio_hub if source == SOURCE_MEDIAHUB else self.radio_device).setChecked(True)

        layout.addWidget(self.radio_device)
        self.device_box = QWidget()
        device_layout = QVBoxLayout(self.device_box)
        device_layout.setContentsMargins(24, 0, 0, 8)
        device_intro = QLabel(
            "Studio liest die Karten direkt vom ESPuino und kann neue Karten zuweisen: Karte auflegen, "
            "Ordner wählen, fertig. Die Dateien liest Studio von der SD-Karte des ESPuino im Kartenleser "
            "(oder einer Kopie davon), nicht über das WLAN.")
        device_intro.setWordWrap(True)
        device_layout.addWidget(device_intro)
        device_form = QFormLayout()
        self.host_edit = QLineEdit(host)
        self.host_edit.setPlaceholderText("z. B. 192.168.178.20 – steht in der Weboberfläche des ESPuino")
        test_row = QWidget()
        test_layout = QHBoxLayout(test_row)
        test_layout.setContentsMargins(0, 0, 0, 0)
        test_layout.addWidget(self.host_edit, 1)
        self.btn_test = QPushButton("Verbindung testen")
        self.btn_test.setObjectName("ghostButton")
        self.btn_test.clicked.connect(self.check_device)
        test_layout.addWidget(self.btn_test)
        device_form.addRow("Adresse:", test_row)
        self.test_label = QLabel()
        self.test_label.setWordWrap(True)
        device_form.addRow("", self.test_label)
        self.sd_edit = QLineEdit(sd_root)
        self.sd_edit.setPlaceholderText("Laufwerk der SD-Karte, z. B. E:\\ – für Cover und zum Zuweisen")
        device_form.addRow("SD-Karte:", _with_browse(self.sd_edit, self._browse_sd))
        device_layout.addLayout(device_form)
        layout.addWidget(self.device_box)

        layout.addWidget(self.radio_hub)
        self.hub_box = QWidget()
        hub_layout = QVBoxLayout(self.hub_box)
        hub_layout.setContentsMargins(24, 0, 0, 0)
        intro = QLabel(
            "Für Karten, die der ESPuino-Mediahub verwaltet. Studio liest die Datei <b>db.json</b> im "
            "Datenordner des Mediahub (z. B. auf dem NAS). Dazu die Mediathek wählen, die der Mediahub als "
            "<i>/media</i> nutzt – derselbe Ordner, nur so, wie er auf diesem PC erreichbar ist. "
            "Am Mediahub wird nichts geändert.")
        intro.setWordWrap(True)
        hub_layout.addWidget(intro)
        form = QFormLayout()
        self.db_edit = QLineEdit(db_path)
        self.db_edit.setPlaceholderText(f"…/mediahub-data/{DB_FILENAME}")
        form.addRow("db.json:", _with_browse(self.db_edit, self._browse_db))
        self.media_edit = QLineEdit(media_root)
        self.media_edit.setPlaceholderText("Ordner mit Musik und Hörspielen")
        form.addRow("Mediathek:", _with_browse(self.media_edit, self._browse_media))
        hub_layout.addLayout(form)
        layout.addWidget(self.hub_box)

        note = QLabel("Kartencover liegen im Datenordner von Studio und gehören zur Kartennummer – "
                      "egal, aus welcher Quelle die Karte gelesen wird.")
        note.setWordWrap(True)
        note.setStyleSheet(f"color: {theme.OVERLAY2};")
        layout.addWidget(note)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Übernehmen")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("Abbrechen")
        buttons.accepted.connect(self.try_accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.radio_device.toggled.connect(self._update_enabled)
        self._update_enabled()

    def _update_enabled(self, *_):
        self.device_box.setEnabled(self.radio_device.isChecked())
        self.hub_box.setEnabled(self.radio_hub.isChecked())

    def _browse_sd(self):
        path = QFileDialog.getExistingDirectory(self, "SD-Karte des ESPuino wählen", self.sd_edit.text().strip())
        if path:
            self.sd_edit.setText(path)

    def _browse_db(self):
        start = os.path.dirname(self.db_edit.text().strip()) or ""
        path, _ = QFileDialog.getOpenFileName(self, "db.json des Mediahub wählen", start,
                                              f"Mediahub-Datenbank ({DB_FILENAME});;JSON (*.json)")
        if path:
            self.db_edit.setText(path)

    def _browse_media(self):
        path = QFileDialog.getExistingDirectory(self, "Mediathek wählen", self.media_edit.text().strip())
        if path:
            self.media_edit.setText(path)

    @property
    def source(self) -> str:
        return SOURCE_MEDIAHUB if self.radio_hub.isChecked() else SOURCE_DEVICE

    @property
    def host(self) -> str:
        return self.host_edit.text().strip()

    @property
    def sd_root(self) -> str:
        return self.sd_edit.text().strip()

    @property
    def db_path(self) -> str:
        return self.db_edit.text().strip()

    @property
    def media_root(self) -> str:
        return self.media_edit.text().strip()

    def check_device(self) -> bool:
        """Adresse pruefen (GET /info); Ergebnis steht in test_label"""
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            host = normalize_host(self.host)
            info = fetch_info(host)
        except DeviceError as exc:
            self.test_label.setText(str(exc))
            self.test_label.setStyleSheet(f"color: {theme.YELLOW};")
            return False
        finally:
            QApplication.restoreOverrideCursor()
        self.host_edit.setText(host)
        self.test_label.setText(f"Verbunden – Firmware {firmware_label(info)}")
        self.test_label.setStyleSheet(f"color: {theme.GREEN};")
        return True

    def try_accept(self):
        if self.source == SOURCE_DEVICE:
            if not self.check_device():
                QMessageBox.warning(self, "ESPuino einrichten", self.test_label.text())
                return
            if self.sd_root and not os.path.isdir(self.sd_root):
                QMessageBox.warning(self, "ESPuino einrichten", "Die SD-Karte ist kein vorhandener Ordner. "
                                    "Feld leer lassen, wenn sie gerade nicht im Kartenleser steckt.")
                return
            self.accept()
            return
        try:
            data = load_cards(self.db_path)
        except MediahubError as exc:
            QMessageBox.warning(self, "ESPuino einrichten", str(exc))
            return
        if not os.path.isdir(self.media_root):
            QMessageBox.warning(self, "ESPuino einrichten", "Bitte die Mediathek als vorhandenen Ordner wählen.")
            return
        self.db_edit.setText(str(db_file(self.db_path)))
        self.card_count = len(data.cards)
        self.accept()


class AssignCardDialog(QDialog):
    """Neue Karte direkt auf dem ESPuino zuweisen: Studio hoert am Websocket des
    ESPuino mit, die aufgelegte Karte erscheint hier; dann Abspielmodus und Ordner
    waehlen und mit POST /rfid speichern, wie in der Weboberflaeche des ESPuino."""

    def __init__(self, host: str, sd_root: str, known: Dict[str, str], parent=None, listen: bool = True):
        super().__init__(parent)
        self.host = host
        self.sd_root = sd_root if sd_root and os.path.isdir(sd_root) else ""
        self.known = known  # Kartennummer -> Name, fuer die Warnung "schon belegt"
        self.result_entry: Optional[dict] = None
        self.setWindowTitle("Karte zuweisen")
        self.setMinimumWidth(560)
        layout = QVBoxLayout(self)
        self.status_label = QLabel("Verbinde mit dem ESPuino …")
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)

        form = QFormLayout()
        self.card_edit = QLineEdit()
        self.card_edit.setPlaceholderText("erscheint, sobald eine Karte auf dem ESPuino liegt")
        self.card_edit.textChanged.connect(self._update_card_hint)
        form.addRow("Karte:", self.card_edit)
        self.card_hint = QLabel()
        self.card_hint.setWordWrap(True)
        self.card_hint.setStyleSheet(f"color: {theme.YELLOW};")
        self.card_hint.hide()
        form.addRow("", self.card_hint)
        self.mode_combo = QComboBox()
        for mode in ASSIGNABLE_MODES:
            self.mode_combo.addItem(play_mode_label(str(mode)), mode)
        self.mode_combo.currentIndexChanged.connect(self._update_path_hint)
        form.addRow("Abspielmodus:", self.mode_combo)
        self.path_edit = QLineEdit()
        self.path_edit.textChanged.connect(self._update_path_hint)
        self.btn_browse = QPushButton("Wählen …")
        self.btn_browse.setObjectName("ghostButton")
        self.btn_browse.setEnabled(bool(self.sd_root))
        self.btn_browse.setToolTip("Auf der SD-Karte im Kartenleser wählen" if self.sd_root
                                   else "SD-Karte unter „ESPuino einrichten“ angeben, dann geht das per Auswahl")
        self.btn_browse.clicked.connect(self._browse)
        path_row = QWidget()
        path_layout = QHBoxLayout(path_row)
        path_layout.setContentsMargins(0, 0, 0, 0)
        path_layout.addWidget(self.path_edit, 1)
        path_layout.addWidget(self.btn_browse)
        form.addRow("Ordner/Datei:", path_row)
        self.path_hint = QLabel()
        self.path_hint.setWordWrap(True)
        self.path_hint.setStyleSheet(f"color: {theme.OVERLAY2};")
        self.path_hint.hide()
        form.addRow("", self.path_hint)
        layout.addLayout(form)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        self.btn_ok = buttons.button(QDialogButtonBox.StandardButton.Ok)
        self.btn_ok.setText("Zuweisen")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("Abbrechen")
        buttons.accepted.connect(self.try_accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self._update_path_hint()

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

    def _on_connected(self):
        self.status_label.setText("Bereit: Bitte die Karte auf den ESPuino legen.")

    def _on_disconnected(self):
        if not self.isVisible():
            return
        self.status_label.setText("Keine Verbindung zum ESPuino. Die Kartennummer lässt sich auch von Hand "
                                  "eintragen (sie steht in seiner Weboberfläche).")

    def on_message(self, text: str):
        try:
            message = json.loads(text)
        except ValueError:
            return
        card_id = str(message.get("rfidId") or "").strip() if isinstance(message, dict) else ""
        if card_id.isdigit():
            self.card_edit.setText(card_id)
            self.status_label.setText(f"Karte {format_card_id(card_id)} erkannt. "
                                      "Eine andere Karte auflegen, um sie zu wechseln.")

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

    def _local(self, sd_path: str) -> str:
        return os.path.join(self.sd_root, *[p for p in sd_path.split("/") if p])

    def _update_card_hint(self, *_):
        name = self.known.get(self.card_id, "")
        self.card_hint.setText(f"Diese Karte ist schon belegt: „{name}“. Zuweisen ersetzt das." if name else "")
        self.card_hint.setVisible(bool(name))

    def _update_path_hint(self, *_):
        single = self.play_mode in FILE_MODES
        self.path_edit.setPlaceholderText("/Ordner/titel.mp3 auf der SD-Karte" if single
                                          else "/Ordner auf der SD-Karte")
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
            self.path_edit.setText(sd_path_from_local(self.sd_root, path))
        except DeviceError as exc:
            QMessageBox.warning(self, "Karte zuweisen", str(exc))

    def try_accept(self):
        if not self.card_id.isdigit():
            QMessageBox.information(self, "Karte zuweisen", "Noch keine Karte: bitte auf den ESPuino legen.")
            return
        if not self.sd_path:
            QMessageBox.information(self, "Karte zuweisen", "Bitte einen Ordner (oder eine Datei) wählen.")
            return
        name = self.known.get(self.card_id)
        if name:
            answer = QMessageBox.question(self, "Karte ersetzen?",
                                          f"Karte {format_card_id(self.card_id)} spielt bisher „{name}“. "
                                          f"Stattdessen „{self.sd_path}“ zuweisen?")
            if answer != QMessageBox.StandardButton.Yes:
                return
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            self.result_entry = assign_card(self.host, self.card_id, self.sd_path, self.play_mode)
        except DeviceError as exc:
            QApplication.restoreOverrideCursor()
            QMessageBox.warning(self, "Karte zuweisen", str(exc))
            return
        QApplication.restoreOverrideCursor()
        self.accept()


class TransferWorker(QThread):
    """Kopiert bzw. laedt die Plaene nacheinander hoch (core/espuino_transfer.py).
    assignments: je Plan None oder (Kartennummer, Abspielmodus); die Karte wird
    erst zugewiesen, wenn alle Dateien ihres Plans auf der SD-Karte sind."""
    progress = pyqtSignal(int, int, str)  # Bytes fertig, Bytes gesamt, Text
    failed = pyqtSignal(str)
    done = pyqtSignal(int, int, bool)  # kopiert, uebersprungen, abgebrochen
    assigned = pyqtSignal(str, str, int)  # Kartennummer, Ordner, Abspielmodus

    def __init__(self, plans, sd_root: str, host: str, parent=None, assignments=None):
        super().__init__(parent)
        self.plans = plans
        self.sd_root = sd_root
        self.host = host
        self.assignments = assignments or [None] * len(plans)
        self._cancel = False

    def cancel(self):
        self._cancel = True

    def run(self):
        total = sum(p.total for p in self.plans)
        offset = 0
        copied = skipped = 0
        try:
            for plan, assignment in zip(self.plans, self.assignments):
                def report(done, _total, name, offset=offset, folder=plan.folder):
                    self.progress.emit(offset + done, total, f"{folder}/{name}" if name else folder)
                if self.sd_root:
                    result = copy_to_sd(plan, self.sd_root, report, lambda: self._cancel)
                else:
                    result = upload_to_device(plan, self.host, report, lambda: self._cancel)
                copied += result.copied
                skipped += result.skipped
                offset += plan.total
                if result.cancelled:
                    self.done.emit(copied, skipped, True)
                    return
                if assignment is not None:
                    card_id, play_mode = assignment
                    assign_card(self.host, card_id, plan.folder, play_mode)
                    self.assigned.emit(card_id, plan.folder, play_mode)
        except DeviceError as exc:
            self.failed.emit(str(exc))
            return
        self.done.emit(copied, skipped, False)


class TransferDialog(QDialog):
    """Vor dem Uebertragen: Zielordner und Weg (SD-Karte im PC oder WLAN)"""

    WLAN_BYTES_PER_SECOND = 400 * 1024  # grobe Schaetzung fuer den Hinweis

    def __init__(self, albums, sd_root: str, host: str, parent=None):
        super().__init__(parent)
        self.albums = albums  # LibraryAlbum
        self.sd_root = sd_root if sd_root and os.path.isdir(sd_root) else ""
        self.host = host
        self.setWindowTitle("Auf den ESPuino übertragen")
        self.setMinimumWidth(560)
        layout = QVBoxLayout(self)
        form = QFormLayout()
        self.folder_edit = QLineEdit()
        if len(albums) == 1:
            self.folder_edit.setText("/" + safe_name(albums[0].name))
            form.addRow("Zielordner:", self.folder_edit)
        else:
            self.folder_edit.hide()
            form.addRow("Alben:", QLabel(f"{len(albums)} Alben, je ein Ordner mit dem Albumnamen"))
        size = sum(os.path.getsize(t) for a in albums for t in a.tracks if os.path.isfile(t))
        if self.sd_root:
            way = f"Kopieren auf die SD-Karte im Kartenleser ({self.sd_root})"
        else:
            minutes = max(1, round(size / self.WLAN_BYTES_PER_SECOND / 60))
            way = (f"Über das WLAN zum ESPuino ({host}) – das dauert, etwa {minutes} Minute"
                   f"{'' if minutes == 1 else 'n'}. Schneller geht es mit der SD-Karte im Kartenleser.")
        way_label = QLabel(way)
        way_label.setWordWrap(True)
        form.addRow("Weg:", way_label)
        form.addRow("Umfang:", QLabel(f"{sum(len(a.tracks) for a in albums)} Dateien, {size / 1e6:.0f} MB"))
        layout.addLayout(form)
        self.assign_check = QCheckBox("Danach eine Karte für dieses Album zuweisen")
        self.assign_check.setChecked(True)
        self.assign_check.setVisible(len(albums) == 1 and bool(host))
        layout.addWidget(self.assign_check)
        note = QLabel("Dateien, die es im Zielordner schon gibt, werden übersprungen.")
        note.setStyleSheet(f"color: {theme.OVERLAY2};")
        layout.addWidget(note)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Übertragen")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("Abbrechen")
        buttons.accepted.connect(self.try_accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.plans = []

    @property
    def assign_after(self) -> bool:
        return not self.assign_check.isHidden() and self.assign_check.isChecked()

    def try_accept(self):
        try:
            if len(self.albums) == 1:
                self.plans = [plan_album(self.albums[0].tracks, self.folder_edit.text())]
            else:
                self.plans = [plan_album(a.tracks, "/" + safe_name(a.name)) for a in self.albums]
        except DeviceError as exc:
            QMessageBox.warning(self, "Auf den ESPuino übertragen", str(exc))
            return
        self.accept()
