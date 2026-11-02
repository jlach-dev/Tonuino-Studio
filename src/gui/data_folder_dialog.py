"""
Datenordner der Bibliothek wechseln: Daten mitnehmen oder einen vorhandenen
Datenordner verwenden. Der bisherige Ordner bleibt immer stehen.
"""

import os
from pathlib import Path
from typing import Optional

from PyQt6.QtCore import QSettings
from PyQt6.QtWidgets import QDialog, QFileDialog, QHBoxLayout, QLabel, QProgressBar, QPushButton, QVBoxLayout

from core import ai_settings, data_folder
from core.data_folder import DataFolderError
from gui.common import confirm_action
from gui.cover_manager_dialog import SETTINGS_DATA_DIR
from gui.workers import BackgroundTask


class DataFolderDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.current = QSettings().value(SETTINGS_DATA_DIR, "", type=str)
        self.changed = False
        self._task: Optional[BackgroundTask] = None
        self._target = ""
        self.setWindowTitle("Datenordner der Bibliothek")
        self.setMinimumWidth(520)
        self._setup_ui()

    def _setup_ui(self):
        layout = QVBoxLayout(self)
        intro = QLabel("Hier liegen Cover-Archiv, Serienprofile und die Übersicht der Bibliothek. "
                       "Deine Hörspiele und die SD-Karte sind davon nicht betroffen.")
        intro.setWordWrap(True)
        layout.addWidget(intro)
        self.path_label = QLabel()
        self.path_label.setObjectName("folderMeta")
        self.path_label.setWordWrap(True)
        layout.addWidget(self.path_label)
        self.progress = QProgressBar()
        self.progress.setVisible(False)
        layout.addWidget(self.progress)
        self.status_label = QLabel("")
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        self.btn_choose = QPushButton("Anderen Ordner wählen …")
        self.btn_choose.clicked.connect(self._choose)
        buttons.addWidget(self.btn_choose)
        self.btn_cancel = QPushButton("Abbrechen")
        self.btn_cancel.setVisible(False)
        self.btn_cancel.clicked.connect(self._cancel)
        buttons.addWidget(self.btn_cancel)
        self.btn_close = QPushButton("Schließen")
        self.btn_close.clicked.connect(self.accept)
        buttons.addWidget(self.btn_close)
        layout.addLayout(buttons)
        self._show_current()

    def _show_current(self):
        self.path_label.setText(f"Aktuell: {self.current}" if self.current else "Aktuell: noch nicht festgelegt")

    def _busy(self) -> bool:
        return self._task is not None and self._task.isRunning()

    def _choose(self):
        start = str(Path(self.current).parent) if self.current else ""
        path = QFileDialog.getExistingDirectory(self, "Neuer Datenordner", start)
        if path:
            self.use_folder(path)

    def use_folder(self, new: str):
        old = self.current
        try:
            data_folder.validate_target(old, new)
        except DataFolderError as e:
            self.status_label.setText(str(e))
            return
        if data_folder.has_data(new):
            if confirm_action(self, "Datenordner verwenden",
                              f"In {new} liegt schon ein Datenordner. Die App verwendet ihn, wie er ist, und übernimmt "
                              "nichts aus dem bisherigen Ordner. Verwenden?"):
                self._switch(new, "Vorhandener Datenordner wird verwendet, kopiert wurde nichts.")
            return
        if not old or not data_folder.has_data(old):
            unreachable = bool(old) and not os.path.isdir(old)
            self._switch(new, "Der bisherige Ordner ist nicht erreichbar, kopiert wurde nichts." if unreachable
                         else "Es gab noch keine Daten zu kopieren.")
            return
        try:
            count, size = data_folder.measure(old)
        except OSError as e:
            self.status_label.setText(f"Der bisherige Ordner lässt sich nicht lesen: {e}")
            return
        if confirm_action(self, "Daten kopieren",
                          f"{count} Dateien ({size / (1024 * 1024):.0f} MB) werden nach {new} kopiert und danach geprüft. "
                          "Der bisherige Ordner bleibt unverändert, du kannst ihn später selbst löschen. Fortfahren?",
                          default_no=False):
            self._start_copy(old, new)

    def _start_copy(self, old: str, new: str):
        self._target = new
        task = self._task = BackgroundTask(lambda t: data_folder.copy_data(
            old, new, lambda i, total, name: t.progress.emit(i, total, name), lambda: t.cancelled))
        task.done.connect(self._on_copied)
        task.failed.connect(self._on_failed)
        task.progress.connect(self._on_progress)
        self._set_copying(True)
        self.status_label.setText("Kopiere die Daten …")
        task.start()

    def _set_copying(self, copying: bool):
        self.progress.setVisible(copying)
        self.btn_cancel.setVisible(copying)
        self.btn_cancel.setEnabled(True)
        self.btn_choose.setEnabled(not copying)
        self.btn_close.setEnabled(not copying)

    def _on_progress(self, index: int, total: int, name: str):
        self.progress.setRange(0, total)
        self.progress.setValue(index)
        self.status_label.setText(f"{index} von {total}: {name}")

    def _cancel(self):
        if self._task is not None:
            self._task.cancel()
        self.btn_cancel.setEnabled(False)
        self.status_label.setText("Wird nach der aktuellen Datei abgebrochen …")

    def _on_copied(self, finished: bool):
        self._set_copying(False)
        if finished:
            self._switch(self._target, "Die Daten sind kopiert und geprüft. Der bisherige Ordner kann gelöscht werden, "
                                       "wenn du ihn nicht mehr brauchst.")
        else:
            self.status_label.setText("Abgebrochen. Der Datenordner bleibt unverändert.")

    def _on_failed(self, message: str):
        self._set_copying(False)
        self.status_label.setText(f"Das hat nicht geklappt: {message} Der Datenordner bleibt unverändert.")

    def _switch(self, new: str, note: str):
        try:
            Path(new).mkdir(parents=True, exist_ok=True)
        except OSError as e:
            self.status_label.setText(f"Der Ordner lässt sich nicht anlegen: {e}")
            return
        settings = QSettings()
        fpcalc = settings.value(ai_settings.SETTINGS_FPCALC, "", type=str)
        settings.setValue(ai_settings.SETTINGS_FPCALC, data_folder.moved_path(fpcalc, self.current, new))
        settings.setValue(SETTINGS_DATA_DIR, new)
        self.current = new
        self.changed = True
        self._show_current()
        self.status_label.setText(note)

    def done(self, result):
        if self._busy():  # erst sauber abbrechen, sonst bliebe der Zwischenordner liegen
            self._cancel()
            return
        super().done(result)
