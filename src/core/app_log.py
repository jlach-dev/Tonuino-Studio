"""
Logdatei von Tonuino-Studio

Die EXE laeuft ohne Konsole (console=False in Tonuino-Manager.spec) - ohne
Logdatei waeren Warnungen und Abstuerze dort unsichtbar. Das Log rotiert
(einige MB), damit es auf Dauer nicht waechst.

  Windows: %LOCALAPPDATA%/Tonuino-Studio/log/tonuino-studio.log
  macOS:   ~/Library/Logs/Tonuino-Studio/tonuino-studio.log
  Linux:   ~/.local/state/Tonuino-Studio/log/tonuino-studio.log
"""

import logging
import os
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Optional

from core.branding import APP_NAME, PACKAGE_NAME

APP_DIR_NAME = APP_NAME
LOG_FILE_NAME = f"{PACKAGE_NAME}.log"
LOG_MAX_BYTES = 1_000_000
LOG_BACKUP_COUNT = 3

LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s [%(threadName)s]: %(message)s"


def log_dir() -> Path:
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or os.path.join(os.path.expanduser("~"), "AppData", "Local")
        return Path(base) / APP_DIR_NAME / "log"
    if sys.platform == "darwin":
        return Path(os.path.expanduser("~")) / "Library" / "Logs" / APP_DIR_NAME
    base = os.environ.get("XDG_STATE_HOME") or os.path.join(os.path.expanduser("~"), ".local", "state")
    return Path(base) / APP_DIR_NAME / "log"


def setup_logging(level: int = logging.INFO, directory: Optional[Path] = None) -> Optional[Path]:
    """Root-Logger auf Konsole (falls vorhanden) und rotierende Logdatei
    einrichten. Gibt den Pfad der Logdatei zurueck, None wenn sie nicht
    angelegt werden konnte (dann nur Konsole)."""
    root = logging.getLogger()
    root.setLevel(level)
    formatter = logging.Formatter(LOG_FORMAT)

    # Ohne Konsole (PyInstaller, console=False) ist sys.stderr None
    if sys.stderr is not None:
        console = logging.StreamHandler()
        console.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))
        root.addHandler(console)

    path = (directory or log_dir()) / LOG_FILE_NAME
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        handler = RotatingFileHandler(path, maxBytes=LOG_MAX_BYTES, backupCount=LOG_BACKUP_COUNT,
                                      encoding="utf-8")
    except OSError:
        root.warning("Logdatei %s nicht anlegbar - nur Konsolen-Log", path, exc_info=True)
        return None
    handler.setFormatter(formatter)
    root.addHandler(handler)
    return path
