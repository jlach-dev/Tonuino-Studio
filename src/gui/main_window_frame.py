"""
Hauptfenster: rahmenloses Fenster unter Windows, Vollbild, Maximieren und
gespeicherter Fensterzustand (Teil von MainWindow, siehe gui/main_window.py)

Eigene Datei des Forks, damit gui/main_window.py nah am Original bleibt und
beim Nachziehen weniger Konflikte entstehen. Die Methoden laufen als Teil von
MainWindow und greifen auf dessen Attribute zu.
"""

import sys

from PyQt6.QtCore import QSettings


class WindowFrameMixin:
    """Methoden von MainWindow (siehe Modul-Doku)"""

    def showEvent(self, event):
        super().showEvent(event)
        self._enable_resize_frame()

    def _enable_resize_frame(self):
        """Ohne WS_THICKFRAME laesst Windows das rahmenlose Fenster nicht an den
        Raendern ziehen (siehe gui/win_frame.py)"""
        if sys.platform == "win32" and self._title_bar is not None:
            from gui import win_frame
            win_frame.enable_resize_frame(int(self.winId()))

    def toggle_full_screen(self):
        if self.isFullScreen():
            self._leave_full_screen()
        else:
            self._was_maximized = self.isMaximized()
            self.showFullScreen()

    def _leave_full_screen(self):
        if not self.isFullScreen():
            return
        if getattr(self, "_was_maximized", False):
            self.showMaximized()
        else:
            self.showNormal()

    def toggle_maximized(self):
        if self.isFullScreen():
            self._leave_full_screen()
        elif self.isMaximized():
            self.showNormal()
        else:
            self.showMaximized()

    def _restore_window_state(self):
        """Groesse, Position und Maximiert-Zustand der letzten Sitzung"""
        settings = QSettings()
        geometry = settings.value("window/geometry")
        if geometry is not None:
            self.restoreGeometry(geometry)
        if settings.value("window/maximized", False, type=bool):
            self.showMaximized()
        splitter_state = settings.value("window/splitter")
        if splitter_state is not None:
            self._main_splitter.restoreState(splitter_state)

    def _save_window_state(self):
        settings = QSettings()
        settings.setValue("window/maximized", self.isMaximized() or self.isFullScreen())
        settings.setValue("window/splitter", self._main_splitter.saveState())
        if not self.isMaximized() and not self.isFullScreen():
            settings.setValue("window/geometry", self.saveGeometry())
