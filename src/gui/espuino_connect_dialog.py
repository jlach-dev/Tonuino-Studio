"""
Mit welchem ESPuino arbeiten? Liste der eingerichteten Geraete mit Status.

Erscheint beim Start im ESPuino-Modus (Quelle "ESPuino direkt") und ueber
"Verbinden …" in der Seitenleiste. Jedes Geraet wird im Hintergrund kurz
angefragt (GET /info, /settings); wer noch nicht antwortet, wird alle paar
Sekunden erneut gefragt - so kann man den ESPuino einschalten, waehrend das
Fenster offen ist. "Ohne ESPuino weiter" zeigt die bekannten Karten ohne Geraet.

Eigene Datei des Forks (siehe core/branding.py).
"""

import os
from typing import Dict, Optional

from PyQt6.QtCore import QThread, QTimer, Qt, pyqtSignal
from PyQt6.QtGui import QBrush, QColor
from PyQt6.QtWidgets import (
    QApplication, QCheckBox, QDialog, QFileDialog, QFormLayout, QHBoxLayout, QHeaderView, QLabel, QLineEdit,
    QMessageBox, QPushButton, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget,
)

from core import espuino_device as dev
from gui import theme

RETRY_MS = 5000  # nicht erreichbare Geraete so oft erneut fragen
HOST_ROLE = Qt.ItemDataRole.UserRole
_RUNNING = set()  # laufende Abfragen ueberleben den Dialog (Schliessen wartet nicht aufs WLAN)


class ProbeWorker(QThread):
    """Fragt ein Geraet im Hintergrund, ob es antwortet"""
    finished_probe = pyqtSignal(object)  # core.espuino_device.Probe

    def __init__(self, host: str):
        super().__init__()
        self.host = host
        _RUNNING.add(self)
        self.finished.connect(lambda: _RUNNING.discard(self))

    def run(self):
        self.finished_probe.emit(dev.probe(self.host))


def describe(device: dev.SavedDevice, result: Optional[dev.Probe]) -> str:
    """Statustext einer Zeile"""
    if result is None:
        return "wird geprüft …"
    if not result.reachable:
        return "nicht erreichbar"
    parts = ["verbunden"]
    address = []
    if result.hostname and result.hostname.casefold() != device.label.casefold():
        address.append(result.hostname)
    if result.ip and result.ip != device.host:
        address.append(result.ip)
    if address:
        parts.append(", ".join(address))
    if result.firmware:
        parts.append(f"Firmware {result.firmware}")
    return " · ".join(parts)


class DeviceEditDialog(QDialog):
    """Einen ESPuino anlegen oder aendern: Adresse, Name, SD-Karte auf dem PC"""

    def __init__(self, device: Optional[dev.SavedDevice] = None, parent=None):
        super().__init__(parent)
        self.setWindowTitle("ESPuino bearbeiten" if device else "ESPuino hinzufügen")
        self.setMinimumWidth(560)
        layout = QVBoxLayout(self)
        intro = QLabel("Die Adresse steht in der Weboberfläche des ESPuino: seine IP (z. B. 192.168.178.20) "
                       "oder sein Hostname. Studio fragt ihn nur im WLAN, ohne Anmeldung.")
        intro.setWordWrap(True)
        layout.addWidget(intro)
        form = QFormLayout()
        row = QWidget()
        row_layout = QHBoxLayout(row)
        row_layout.setContentsMargins(0, 0, 0, 0)
        self.host_edit = QLineEdit(device.host if device else "")
        self.host_edit.setPlaceholderText("IP-Adresse oder Hostname")
        row_layout.addWidget(self.host_edit, 1)
        self.btn_test = QPushButton("Verbindung testen")
        self.btn_test.setObjectName("ghostButton")
        self.btn_test.clicked.connect(self.check)
        row_layout.addWidget(self.btn_test)
        form.addRow("Adresse:", row)
        self.test_label = QLabel()
        self.test_label.setWordWrap(True)
        form.addRow("", self.test_label)
        self.name_edit = QLineEdit(device.name if device else "")
        self.name_edit.setPlaceholderText("z. B. ESPuino Kinderzimmer – leer: Hostname des ESPuino")
        form.addRow("Name:", self.name_edit)
        sd_row = QWidget()
        sd_layout = QHBoxLayout(sd_row)
        sd_layout.setContentsMargins(0, 0, 0, 0)
        self.sd_edit = QLineEdit(device.sd_root if device else "")
        self.sd_edit.setPlaceholderText("Laufwerk seiner SD-Karte im Kartenleser, z. B. E:\\ – sonst leer")
        sd_layout.addWidget(self.sd_edit, 1)
        browse = QPushButton("Wählen …")
        browse.setObjectName("ghostButton")
        browse.clicked.connect(self._browse_sd)
        sd_layout.addWidget(browse)
        form.addRow("SD-Karte:", sd_row)
        layout.addLayout(form)
        buttons = QHBoxLayout()
        buttons.addStretch(1)
        cancel = QPushButton("Abbrechen")
        cancel.clicked.connect(self.reject)
        buttons.addWidget(cancel)
        self.btn_ok = QPushButton("Speichern")
        self.btn_ok.setObjectName("primaryButton")
        self.btn_ok.setDefault(True)
        self.btn_ok.clicked.connect(self.try_accept)
        buttons.addWidget(self.btn_ok)
        layout.addLayout(buttons)

    def _browse_sd(self):
        path = QFileDialog.getExistingDirectory(self, "SD-Karte des ESPuino wählen", self.sd_edit.text().strip())
        if path:
            self.sd_edit.setText(path)

    @property
    def host(self) -> str:
        return self.host_edit.text().strip()

    @property
    def name(self) -> str:
        return self.name_edit.text().strip()

    @property
    def sd_root(self) -> str:
        return self.sd_edit.text().strip()

    def check(self) -> bool:
        try:
            host = dev.normalize_host(self.host)
        except dev.DeviceError as exc:
            self._show(str(exc), False)
            return False
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            result = dev.probe(host)
        finally:
            QApplication.restoreOverrideCursor()
        self.host_edit.setText(host)
        if not result.reachable:
            self._show(result.error, False)
            return False
        if not self.name and result.hostname:
            self.name_edit.setText(result.hostname)
        self._show(describe(dev.SavedDevice(host, self.name), result).replace("verbunden", "Erreichbar", 1), True)
        return True

    def _show(self, text: str, ok: bool):
        self.test_label.setText(text)
        self.test_label.setStyleSheet(f"color: {theme.GREEN if ok else theme.YELLOW};")

    def try_accept(self):
        try:
            dev.normalize_host(self.host)
        except dev.DeviceError as exc:
            QMessageBox.warning(self, self.windowTitle(), str(exc))
            return
        if self.sd_root and not os.path.isdir(self.sd_root):
            QMessageBox.warning(self, self.windowTitle(), "Die SD-Karte ist kein vorhandener Ordner. "
                                "Feld leer lassen, wenn sie gerade nicht im Kartenleser steckt.")
            return
        if not self.check():
            answer = QMessageBox.question(
                self, self.windowTitle(), "Der ESPuino antwortet gerade nicht. Trotzdem speichern?\n\n"
                "Das passt, wenn er nur ausgeschaltet ist.")
            if answer != QMessageBox.StandardButton.Yes:
                return
        self.accept()


class ConnectDialog(QDialog):
    """Auswahl "Verbinden mit …". Ergebnis: selected_host (leer: ohne ESPuino)"""

    def __init__(self, parent=None, auto_connect: bool = False):
        """auto_connect: sobald das zuletzt benutzte Geraet antwortet, ohne Rueckfrage
        verbinden (Start mit abgewaehltem "Beim Start fragen")"""
        super().__init__(parent)
        self.setWindowTitle("Mit ESPuino verbinden")
        self.setMinimumSize(640, 340)
        self.selected_host = ""
        self.auto_connect = auto_connect
        self._last = dev.current_host()
        self._results: Dict[str, dev.Probe] = {}
        self._workers: Dict[str, ProbeWorker] = {}
        layout = QVBoxLayout(self)
        self.intro = QLabel("Mit welchem ESPuino möchtest du arbeiten? Ist er aus, einfach einschalten – "
                            "Studio fragt alle paar Sekunden erneut.")
        self.intro.setWordWrap(True)
        layout.addWidget(self.intro)
        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["ESPuino", "Adresse", "Status"])
        self.tree.setRootIsDecorated(False)
        self.tree.setAllColumnsShowFocus(True)
        header = self.tree.header()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self.tree.currentItemChanged.connect(self._update_buttons)
        self.tree.itemDoubleClicked.connect(lambda *_: self.try_connect())
        layout.addWidget(self.tree, 1)
        edit_row = QHBoxLayout()
        self.btn_add = QPushButton("Hinzufügen …")
        self.btn_add.clicked.connect(self.add_device)
        edit_row.addWidget(self.btn_add)
        self.btn_edit = QPushButton("Bearbeiten …")
        self.btn_edit.clicked.connect(self.edit_device)
        edit_row.addWidget(self.btn_edit)
        self.btn_remove = QPushButton("Entfernen")
        self.btn_remove.clicked.connect(self.remove_device)
        edit_row.addWidget(self.btn_remove)
        edit_row.addStretch(1)
        self.ask_box = QCheckBox("Beim Start fragen")
        self.ask_box.setToolTip("Aus: Studio verbindet sich beim Start mit dem zuletzt benutzten ESPuino, "
                                "sobald er antwortet, und fragt nur, wenn er aus ist.")
        self.ask_box.setChecked(dev.ask_on_start())
        self.ask_box.toggled.connect(dev.set_ask_on_start)
        edit_row.addWidget(self.ask_box)
        layout.addLayout(edit_row)
        buttons = QHBoxLayout()
        buttons.addStretch(1)
        self.btn_offline = QPushButton("Ohne ESPuino weiter")
        self.btn_offline.setToolTip("Bekannte Karten und Cover bearbeiten; Zuweisen und Übertragen erst nach "
                                    "dem Verbinden")
        self.btn_offline.clicked.connect(self.reject)
        buttons.addWidget(self.btn_offline)
        self.btn_connect = QPushButton("Verbinden")
        self.btn_connect.setObjectName("primaryButton")
        self.btn_connect.setDefault(True)
        self.btn_connect.clicked.connect(self.try_connect)
        buttons.addWidget(self.btn_connect)
        layout.addLayout(buttons)
        self.timer = QTimer(self)
        self.timer.setInterval(RETRY_MS)
        self.timer.timeout.connect(self._probe_unreachable)
        self.timer.start()
        self.refresh()

    # --- Liste ------------------------------------------------------------------------

    def refresh(self, select: str = ""):
        current = select or self.current_host() or self._last
        self.tree.clear()
        devices = dev.saved_devices()
        for device in devices:
            item = QTreeWidgetItem([device.label, device.host, ""])
            item.setData(0, HOST_ROLE, device.host)
            self.tree.addTopLevelItem(item)
            self._show_status(item, device)
            if device.host.casefold() == current.casefold():
                self.tree.setCurrentItem(item)
        if self.tree.currentItem() is None and self.tree.topLevelItemCount():
            self.tree.setCurrentItem(self.tree.topLevelItem(0))
        if not devices:
            self.intro.setText("Noch kein ESPuino eingerichtet. Mit „Hinzufügen …“ seine Adresse im WLAN "
                               "eintragen – oder ohne ESPuino weiterarbeiten.")
            self.btn_add.setObjectName("primaryButton")
            self.btn_add.style().unpolish(self.btn_add)  # Stil nach dem Objektnamen neu anwenden
            self.btn_add.style().polish(self.btn_add)
        for device in devices:
            if device.host not in self._results and device.host not in self._workers:
                self._probe(device.host)
        self._update_buttons()

    def current_host(self) -> str:
        item = self.tree.currentItem()
        return item.data(0, HOST_ROLE) if item is not None else ""

    def _item(self, host: str) -> Optional[QTreeWidgetItem]:
        for i in range(self.tree.topLevelItemCount()):
            item = self.tree.topLevelItem(i)
            if item.data(0, HOST_ROLE) == host:
                return item
        return None

    def _show_status(self, item: QTreeWidgetItem, device: dev.SavedDevice):
        result = self._results.get(device.host)
        item.setText(0, device.label)
        item.setText(2, describe(device, result))
        color = theme.OVERLAY2 if result is None else theme.GREEN if result.reachable else theme.YELLOW
        item.setForeground(2, QBrush(QColor(color)))
        item.setToolTip(2, result.error if result is not None and result.error else "")

    def _update_buttons(self, *_):
        host = self.current_host()
        result = self._results.get(host)
        reachable = result is not None and result.reachable
        self.btn_connect.setEnabled(reachable)
        self.btn_connect.setToolTip("" if reachable else "Erst wenn der gewählte ESPuino antwortet")
        self.btn_edit.setEnabled(bool(host))
        self.btn_remove.setEnabled(bool(host))

    # --- Pruefen ----------------------------------------------------------------------

    def _probe(self, host: str):
        worker = ProbeWorker(host)
        worker.finished_probe.connect(self._on_probe)
        self._workers[host] = worker
        worker.start()

    def _probe_unreachable(self):
        for device in dev.saved_devices():
            result = self._results.get(device.host)
            if device.host not in self._workers and (result is None or not result.reachable):
                self._probe(device.host)

    def _on_probe(self, result: dev.Probe):
        self._workers.pop(result.host, None)
        device = dev.find_device(result.host)
        if device is None:  # inzwischen entfernt
            return
        if result.reachable and not device.name and result.hostname:
            device = dev.remember_device(device.host, name=result.hostname)
        self._results[result.host] = result
        item = self._item(device.host)
        if item is not None:
            self._show_status(item, device)
        self._update_buttons()
        if self.auto_connect and result.reachable and device.host.casefold() == self._last.casefold():
            self.selected_host = device.host
            self.accept()

    # --- Geraete bearbeiten -----------------------------------------------------------

    def add_device(self):
        dialog = DeviceEditDialog(parent=self)
        if dialog.exec():
            dev.remember_device(dialog.host, name=dialog.name, sd_root=dialog.sd_root)
            self._results.pop(dialog.host, None)
            self.refresh(select=dialog.host)

    def edit_device(self):
        device = dev.find_device(self.current_host())
        if device is None:
            return
        dialog = DeviceEditDialog(device, self)
        if not dialog.exec():
            return
        if dialog.host.casefold() != device.host.casefold():
            dev.forget_device(device.host)
            self._results.pop(device.host, None)
        dev.remember_device(dialog.host, name=dialog.name, sd_root=dialog.sd_root)
        self._results.pop(dialog.host, None)
        self.refresh(select=dialog.host)

    def remove_device(self):
        device = dev.find_device(self.current_host())
        if device is None:
            return
        answer = QMessageBox.question(
            self, "ESPuino entfernen", f"„{device.label}“ aus der Liste entfernen?\n\n"
            "Am ESPuino und an den bekannten Karten ändert sich nichts.")
        if answer != QMessageBox.StandardButton.Yes:
            return
        dev.forget_device(device.host)
        self._results.pop(device.host, None)
        self.refresh()

    # --- Ergebnis ---------------------------------------------------------------------

    def try_connect(self):
        host = self.current_host()
        result = self._results.get(host)
        if not host or result is None or not result.reachable:
            return
        self.selected_host = host
        self.accept()

    def done(self, code: int):
        self.timer.stop()
        for worker in self._workers.values():
            worker.finished_probe.disconnect(self._on_probe)
        self._workers.clear()
        super().done(code)
