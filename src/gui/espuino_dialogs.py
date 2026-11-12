"""
Dialoge des ESPuino-Modus: Einrichten (ESPuino direkt im WLAN oder Mediahub)
und Alben auf seine SD-Karte uebertragen (core/espuino_transfer.py). Karten
zuweisen: gui/espuino_card_reader.py.

Eigene Datei des Forks (siehe core/branding.py).
"""

import os

from PyQt6.QtCore import QThread, Qt, pyqtSignal
from PyQt6.QtWidgets import (
    QApplication, QButtonGroup, QCheckBox, QDialog, QDialogButtonBox, QFileDialog, QFormLayout, QHBoxLayout,
    QLabel, QLineEdit, QMessageBox, QPushButton, QRadioButton, QVBoxLayout, QWidget,
)

from core.espuino_device import (
    SOURCE_DEVICE, SOURCE_MEDIAHUB, DeviceError, assign_card, fetch_info, firmware_label, normalize_host,
)
from core.espuino_transfer import copy_to_sd, plan_album, safe_name, upload_to_device
from core.mediahub_cards import DB_FILENAME, MediahubError, db_file, load_cards
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
        self.reachable = False  # nach Uebernehmen: hat der ESPuino geantwortet?
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
            if self.sd_root and not os.path.isdir(self.sd_root):
                QMessageBox.warning(self, "ESPuino einrichten", "Die SD-Karte ist kein vorhandener Ordner. "
                                    "Feld leer lassen, wenn sie gerade nicht im Kartenleser steckt.")
                return
            self.reachable = self.check_device()
            if not self.reachable:
                try:
                    normalize_host(self.host)
                except DeviceError as exc:
                    QMessageBox.warning(self, "ESPuino einrichten", str(exc))
                    return
                # typisch: seine SD-Karte steckt gerade im PC, der ESPuino ist aus
                answer = QMessageBox.question(
                    self, "ESPuino einrichten",
                    self.test_label.text() + "\n\nTrotzdem speichern? Mit der SD-Karte im Kartenleser kannst du "
                    "Alben übertragen und Cover gestalten; Zuweisen geht erst, wenn der ESPuino wieder läuft.")
                if answer != QMessageBox.StandardButton.Yes:
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
