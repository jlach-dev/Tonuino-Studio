"""
"Fehler melden / Feedback": stellt Version, Modus, System und das Ende des Logs
zusammen, damit Beta-Tester eine Meldung ohne GitHub-Kenntnisse abgeben koennen.
Die App sendet nichts selbst - der Text geht in die Zwischenablage, die Knoepfe
oeffnen nur den Forumsthread bzw. ein vorbefuelltes GitHub-Issue im Browser.

Eigene Datei des Forks (siehe core/branding.py).
"""

import platform
import re
from pathlib import Path
from urllib.parse import urlencode

from PyQt6.QtCore import QUrl
from PyQt6.QtGui import QDesktopServices, QGuiApplication
from PyQt6.QtWidgets import (QDialog, QHBoxLayout, QLabel, QPlainTextEdit, QPushButton, QVBoxLayout)

from core import __version__, app_mode
from core.branding import APP_NAME, FEEDBACK_ESPUINO_URL, FEEDBACK_TONUINO_URL, HOMEPAGE

LOG_LINES = 50
_IP = re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}\b")
_CARD_ID = re.compile(r"\b\d{12}\b|\b\d{3} \d{3} \d{3} \d{3}\b")


def anonymize(text: str, home: str = "") -> str:
    """Benutzerordner zu ~, IP-Adressen und Kartennummern unkenntlich"""
    home = home or str(Path.home())
    for variant in {home, home.replace("\\", "/")}:
        if variant:
            text = re.sub(re.escape(variant), "~", text, flags=re.IGNORECASE)
    text = _IP.sub("x.x.x.x", text)
    return _CARD_ID.sub("<Kartennummer>", text)


def log_tail(lines: int = LOG_LINES) -> str:
    from core.app_log import LOG_FILE_NAME, log_dir
    try:
        path = log_dir() / LOG_FILE_NAME
        with open(path, encoding="utf-8", errors="replace") as handle:
            return "".join(handle.readlines()[-lines:])
    except OSError:
        return ""


def system_info(firmware: str = "") -> str:
    mode = app_mode.current_mode()
    lines = [f"{APP_NAME} {__version__}",
             f"Modus: {app_mode.MODE_LABELS.get(mode, mode)}",
             f"System: {platform.system()} {platform.release()} ({platform.machine()}), Python {platform.python_version()}"]
    if firmware:
        lines.append(f"ESPuino-Firmware: {firmware}")
    return "\n".join(lines)


def report_text(firmware: str = "", log: str = None) -> str:
    log = log_tail() if log is None else log
    text = ("Was ist passiert? (bitte ausfüllen)\n\n\n"
            "Was hast du erwartet?\n\n\n"
            "Schritte zum Nachstellen:\n1.\n2.\n\n"
            "--- automatisch ---\n" + system_info(firmware))
    if log.strip():
        text += "\n\n--- Log (letzte Zeilen) ---\n" + log.rstrip()
    return anonymize(text)


def forum_url() -> str:
    return FEEDBACK_ESPUINO_URL if app_mode.current_mode() == app_mode.MODE_ESPUINO else FEEDBACK_TONUINO_URL


def issue_url(firmware: str = "") -> str:
    """Neues GitHub-Issue, vorbefuellt nur mit Version, Modus und System (kein Log in der Adresse)"""
    body = ("**Was ist passiert?**\n\n\n**Was hast du erwartet?**\n\n\n**Schritte zum Nachstellen**\n1.\n2.\n\n"
            "**Umgebung**\n```\n" + anonymize(system_info(firmware)) + "\n```\n"
            "(Log bei Bedarf aus „Fehler melden“ in der App einfügen.)")
    return f"{HOMEPAGE}/issues/new?" + urlencode({"title": "", "body": body})


class FeedbackDialog(QDialog):
    def __init__(self, parent=None, firmware: str = ""):
        super().__init__(parent)
        self.setWindowTitle("Fehler melden / Feedback")
        self.resize(640, 520)
        self.firmware = firmware
        layout = QVBoxLayout(self)
        intro = QLabel("Danke fürs Testen! Beschreib kurz, was passiert ist. Unten stehen Version, Modus und das "
                       "Ende des Protokolls – Benutzerordner, IP-Adressen und Kartennummern sind unkenntlich "
                       "gemacht. Die App sendet nichts selbst: Kopiere den Text und füge ihn im Forum oder bei "
                       "GitHub ein.")
        intro.setWordWrap(True)
        layout.addWidget(intro)
        self.text_edit = QPlainTextEdit(report_text(firmware))
        layout.addWidget(self.text_edit, 1)
        buttons = QHBoxLayout()
        self.btn_copy = QPushButton("In Zwischenablage kopieren")
        self.btn_copy.setObjectName("primaryButton")
        self.btn_copy.clicked.connect(self.copy)
        buttons.addWidget(self.btn_copy)
        self.btn_forum = QPushButton("Im Forum melden …")
        self.btn_forum.setToolTip("Kopiert den Text und öffnet den Forumsthread im Browser")
        self.btn_forum.clicked.connect(lambda: self._open(forum_url()))
        buttons.addWidget(self.btn_forum)
        self.btn_issue = QPushButton("GitHub-Issue öffnen …")
        self.btn_issue.setToolTip("Kopiert den Text und öffnet ein neues Issue bei GitHub (Konto nötig)")
        self.btn_issue.clicked.connect(lambda: self._open(issue_url(firmware)))
        buttons.addWidget(self.btn_issue)
        buttons.addStretch(1)
        close = QPushButton("Schließen")
        close.clicked.connect(self.reject)
        buttons.addWidget(close)
        layout.addLayout(buttons)

    def copy(self):
        QGuiApplication.clipboard().setText(self.text_edit.toPlainText())

    def _open(self, url: str):
        self.copy()
        QDesktopServices.openUrl(QUrl(url))
