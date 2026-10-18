"""
Unbehandelte Ausnahmen abfangen (sys.excepthook / threading.excepthook)

Ohne eigenen Hook beendet PyQt6 die App bei einer Ausnahme in einem Slot
sofort (qFatal) - in der EXE ohne Konsole verschwindet das Fenster dann
kommentarlos. Stattdessen: Traceback ins Log (siehe core/app_log.py), Meldung
im GUI-Thread anzeigen, App weiterlaufen lassen.
"""

import logging
import sys
import threading
import traceback
from pathlib import Path
from typing import Optional

from PyQt6.QtCore import QObject, pyqtSignal
from PyQt6.QtWidgets import QApplication, QMessageBox

log = logging.getLogger(__name__)


class _CrashNotifier(QObject):
    """Bringt die Fehlermeldung in den GUI-Thread: das Signal wird aus
    beliebigen Threads ausgeloest, der Slot laeuft im Thread des Objekts
    (dem GUI-Thread, in dem install_exception_hooks aufgerufen wird)."""
    crashed = pyqtSignal(str, str)  # Kurztext, Traceback

    def __init__(self, log_file: Optional[Path]):
        super().__init__()
        self.log_file = log_file
        self._showing = False
        self.crashed.connect(self._show)

    def _show(self, summary: str, details: str):
        # Folgefehler, waehrend die Meldung noch offen ist, nur loggen statt
        # Dialoge zu stapeln
        if self._showing or QApplication.instance() is None:
            return
        self._showing = True
        try:
            text = f"Ein unerwarteter Fehler ist aufgetreten:\n\n{summary}\n\n"
            if self.log_file is not None:
                text += f"Details stehen in der Logdatei:\n{self.log_file}\n\n"
            text += ("Du kannst weiterarbeiten. Falls sich das Programm danach seltsam verhält, "
                     "bitte neu starten.")
            box = QMessageBox(QMessageBox.Icon.Critical, "Unerwarteter Fehler", text,
                              QMessageBox.StandardButton.Ok, QApplication.activeWindow())
            box.setDetailedText(details)
            box.exec()
        finally:
            self._showing = False


_notifier: Optional[_CrashNotifier] = None


def handle_exception(exc_type, exc_value, exc_tb, thread_name: str = ""):
    """Gemeinsamer Weg fuer beide Hooks: loggen, Meldung anstossen"""
    if issubclass(exc_type, KeyboardInterrupt):
        sys.__excepthook__(exc_type, exc_value, exc_tb)
        return
    where = f" (Thread {thread_name})" if thread_name else ""
    log.critical("Unbehandelte Ausnahme%s", where, exc_info=(exc_type, exc_value, exc_tb))
    if _notifier is None:
        return
    summary = f"{exc_type.__name__}: {exc_value}" if str(exc_value) else exc_type.__name__
    details = "".join(traceback.format_exception(exc_type, exc_value, exc_tb))
    try:
        _notifier.crashed.emit(summary, details)
    except RuntimeError:
        pass  # Notifier schon zerstoert (App wird beendet)


def _sys_hook(exc_type, exc_value, exc_tb):
    # Auch Ausnahmen in QThread.run landen hier - dann im Worker-Thread
    current = threading.current_thread()
    thread_name = "" if current is threading.main_thread() else current.name
    handle_exception(exc_type, exc_value, exc_tb, thread_name)


def _threading_hook(args):
    if args.exc_type is SystemExit:
        return
    handle_exception(args.exc_type, args.exc_value, args.exc_traceback,
                     args.thread.name if args.thread is not None else "")


def install_exception_hooks(log_file: Optional[Path] = None):
    """Nach dem Anlegen der QApplication im GUI-Thread aufrufen"""
    global _notifier
    _notifier = _CrashNotifier(log_file)
    sys.excepthook = _sys_hook
    threading.excepthook = _threading_hook
