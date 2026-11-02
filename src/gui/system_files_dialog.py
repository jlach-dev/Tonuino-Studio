"""
TonUINO-Systemdateien einrichten oder aktualisieren: Firmware-Version waehlen
oder von der Box lesen, Karte mit dem Release vergleichen, fehlende Dateien laden
"""

import logging
from pathlib import Path
from typing import Callable, Optional

from PyQt6.QtCore import QThread, pyqtSignal
from PyQt6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QFormLayout, QHBoxLayout, QLabel, QProgressBar, QPushButton, QVBoxLayout,
)

from core import system_files
from core.system_files import LANGUAGES, FirmwareVersion, SyncPlan
from core.tonuino_serial import TonuinoSerial
from gui.common import confirm_action
from gui.workers import retire_worker

log = logging.getLogger(__name__)


class _Task(QThread):
    """Fuehrt fn(task) im Hintergrund aus"""
    done = pyqtSignal(object)
    failed = pyqtSignal(str)
    progress = pyqtSignal(int, int, str)

    def __init__(self, fn: Callable):
        super().__init__()
        self.fn = fn
        self._cancelled = False

    def cancel(self):
        self._cancelled = True

    @property
    def cancelled(self) -> bool:
        return self._cancelled

    def run(self):
        try:
            self.done.emit(self.fn(self))
        except Exception as e:
            log.exception("Systemdateien: Vorgang fehlgeschlagen")
            self.failed.emit(str(e) or type(e).__name__)


class SystemFilesDialog(QDialog):
    def __init__(self, sd_root: Path, parent=None):
        super().__init__(parent)
        self.sd_root = Path(sd_root)
        self.card_changed = False
        self._written_note = ""
        self.plan: Optional[SyncPlan] = None
        self._task: Optional[_Task] = None
        self._writing = False
        self._loading = True
        self.setWindowTitle("TonUINO-Systemdateien")
        self.setMinimumWidth(560)
        self._setup_ui()
        self._load_releases()

    def _setup_ui(self):
        layout = QVBoxLayout(self)
        intro = QLabel("Die Ordner mp3 und advert enthalten die Sprachansagen der Box. Sie müssen zur Firmware "
                       "passen, sonst spielt die Box falsche Ansagen. Dateien, die nur auf der Karte liegen, "
                       "bleiben erhalten.")
        intro.setWordWrap(True)
        layout.addWidget(intro)

        form = QFormLayout()
        self.release_combo = QComboBox()
        self.release_combo.currentIndexChanged.connect(self._compare)
        form.addRow("Firmware:", self.release_combo)
        self.language_combo = QComboBox()
        for label, directory in LANGUAGES.items():
            self.language_combo.addItem(label, directory)
        self.language_combo.currentIndexChanged.connect(self._compare)
        form.addRow("Sprache:", self.language_combo)

        detect_row = QHBoxLayout()
        self.port_combo = QComboBox()
        for port in TonuinoSerial.list_ports():
            self.port_combo.addItem(f"{port.device} – {port.description}", port.device)
        detect_row.addWidget(self.port_combo, 1)
        self.btn_detect = QPushButton("Von der Box lesen")
        self.btn_detect.setObjectName("ghostButton")
        self.btn_detect.setToolTip("Öffnet die USB-Verbindung; die Box startet dabei meist neu")
        self.btn_detect.clicked.connect(self._detect)
        detect_row.addWidget(self.btn_detect)
        form.addRow("Box per USB:", detect_row)
        layout.addLayout(form)

        self.detect_label = QLabel("Ohne Box bitte die Firmware-Version selbst wählen; vorgewählt ist das neueste Release.")
        self.detect_label.setWordWrap(True)
        layout.addWidget(self.detect_label)

        self.summary_label = QLabel("")
        self.summary_label.setWordWrap(True)
        layout.addWidget(self.summary_label)
        self.replace_check = QCheckBox("Abweichende Dateien ersetzen")
        self.replace_check.setChecked(True)
        self.replace_check.toggled.connect(self._update_buttons)
        layout.addWidget(self.replace_check)
        self.progress = QProgressBar()
        self.progress.setVisible(False)
        layout.addWidget(self.progress)
        self.status_label = QLabel("")
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)

        buttons = QHBoxLayout()
        self.btn_retry = QPushButton("Neu vergleichen")
        self.btn_retry.setObjectName("ghostButton")
        self.btn_retry.clicked.connect(self._retry)
        buttons.addWidget(self.btn_retry)
        buttons.addStretch(1)
        self.btn_write = QPushButton("Dateien schreiben")
        self.btn_write.clicked.connect(self._write)
        buttons.addWidget(self.btn_write)
        self.btn_cancel = QPushButton("Abbrechen")
        self.btn_cancel.clicked.connect(self._cancel_write)
        self.btn_cancel.setVisible(False)
        buttons.addWidget(self.btn_cancel)
        self.btn_close = QPushButton("Schließen")
        self.btn_close.clicked.connect(self.accept)
        buttons.addWidget(self.btn_close)
        layout.addLayout(buttons)
        self._update_buttons()

    def _load_releases(self):
        self._run(lambda task: system_files.fetch_releases(), self._on_releases, "Lade die Liste der Releases …")

    def _retry(self):
        if self.release_combo.count():
            self._compare()
        else:
            self._load_releases()

    def _busy(self) -> bool:
        return self._task is not None and self._task.isRunning()

    def _run(self, fn: Callable, on_done: Callable, status: str):
        retire_worker(self._task)
        self.status_label.setText(status)
        self._task = _Task(fn)
        self._task.done.connect(on_done)
        self._task.failed.connect(self._on_failed)
        self._task.finished.connect(self._update_buttons)
        self._task.start()
        self._update_buttons()

    def _update_buttons(self, *_):
        busy = self._busy()
        files = self._files_to_write()
        self.btn_write.setEnabled(not busy and bool(files))
        self.btn_write.setText(f"{len(files)} Dateien schreiben" if files else "Dateien schreiben")
        self.btn_detect.setEnabled(not busy and self.port_combo.count() > 0 and not self._writing)
        self.btn_retry.setEnabled(not busy)
        self.release_combo.setEnabled(not self._writing)
        self.language_combo.setEnabled(not self._writing)
        self.replace_check.setEnabled(not self._writing)

    def _files_to_write(self) -> list:
        if self.plan is None:
            return []
        return self.plan.missing + (self.plan.changed if self.replace_check.isChecked() else [])

    def _on_failed(self, message: str):
        self._set_writing(False)
        self.plan = None
        self.summary_label.setText("")
        self.status_label.setText(f"Das hat nicht geklappt: {message}")
        self._update_buttons()

    def _on_releases(self, tags: list):
        self._loading = True
        self.release_combo.clear()
        self.release_combo.addItems(tags)
        self._loading = False
        self._compare()

    def _compare(self, *_):
        if self._loading or not self.release_combo.currentText():
            return
        self.plan = None
        self.summary_label.setText("")
        tag = self.release_combo.currentText()
        language_dir = self.language_combo.currentData()
        root = self.sd_root

        def work(task):
            return system_files.compare(root, system_files.fetch_manifest(tag, language_dir))

        self._run(work, self._on_compared, "Vergleiche die Karte mit dem Release …")

    def _on_compared(self, plan: SyncPlan):
        self.plan = plan
        self.summary_label.setText(f"Fehlt: {len(plan.missing)} · Abweichend: {len(plan.changed)} · "
                                   f"Identisch: {plan.same} · Nur auf der Karte: {len(plan.only_on_card)}")
        state = "Die Karte ist auf dem Stand dieses Releases." if not plan.missing and not plan.changed else ""
        self.status_label.setText(" ".join(part for part in (self._written_note, state) if part))
        self._written_note = ""
        self._update_buttons()

    def _detect(self):
        port = self.port_combo.currentData()
        self._run(lambda task: system_files.detect_firmware(port), self._on_detected, f"Lese die Version über {port} …")

    def _on_detected(self, version: Optional[FirmwareVersion]):
        self.status_label.setText("")
        if version is None:
            self.detect_label.setText("Die Box hat keine Version gemeldet. Trenne sie kurz vom Strom und lies "
                                      "direkt danach erneut, oder wähle die Firmware selbst.")
            self._compare()
            return
        tags = [self.release_combo.itemText(i) for i in range(self.release_combo.count())]
        tag, exact = system_files.match_release(tags, version)
        if tag is None:
            self.detect_label.setText(f"Erkannt: {version.label}. Dazu gibt es kein passendes Release, "
                                      "bitte selbst wählen.")
            self._compare()
            return
        self.detect_label.setText(f"Erkannt: {version.label}. " + ("Das passende Release ist ausgewählt." if exact else
                                  f"Kein Release mit genau diesem Datum, ausgewählt ist {tag}. Bitte prüfen."))
        if self.release_combo.currentText() == tag:
            self._compare()
        else:
            self.release_combo.setCurrentText(tag)

    def _write(self):
        files = self._files_to_write()
        changed = len(self.plan.changed) if self.replace_check.isChecked() else 0
        if changed and not confirm_action(
                self, "Dateien ersetzen", f"{changed} Dateien auf der Karte weichen vom Release ab und werden "
                                          "überschrieben. Eigene Aufnahmen darunter gehen dabei verloren. Fortfahren?"):
            return
        tag = self.release_combo.currentText()
        language_dir = self.language_combo.currentData()
        root = self.sd_root

        def work(task):
            return system_files.write_files(root, tag, language_dir, files,
                                            lambda i, total, name: task.progress.emit(i, total, name),
                                            lambda: task.cancelled)

        self.card_changed = True
        self._set_writing(True)
        self.progress.setRange(0, len(files))
        self.progress.setValue(0)
        self._run(work, self._on_written, "Schreibe die Dateien …")
        self._task.progress.connect(self._on_progress)

    def _set_writing(self, writing: bool):
        self._writing = writing
        self.progress.setVisible(writing)
        self.btn_cancel.setVisible(writing)

    def _on_progress(self, index: int, total: int, name: str):
        self.progress.setValue(index)
        self.status_label.setText(f"{index} von {total}: {name}")

    def _cancel_write(self):
        if self._task is not None:
            self._task.cancel()
        self.status_label.setText("Wird nach der aktuellen Datei abgebrochen …")

    def _on_written(self, count: int):
        self._written_note = f"{count} Dateien geschrieben."
        self._set_writing(False)
        self._compare()

    def done(self, result):
        retire_worker(self._task)
        self._task = None
        super().done(result)
