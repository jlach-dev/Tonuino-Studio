"""Fehler melden: Text mit Version und Modus, ohne Benutzerordner, IPs und Kartennummern"""
from urllib.parse import parse_qs, urlparse

import pytest

pytest.importorskip("PyQt6")
from PyQt6.QtCore import QCoreApplication, QSettings
from PyQt6.QtWidgets import QApplication

from core import __version__, app_mode
from gui import feedback_dialog as fb


@pytest.fixture(scope="module")
def app():
    QCoreApplication.setOrganizationName("Tonuino-Manager-Tests")
    QCoreApplication.setApplicationName("tests-feedback")
    application = QApplication.instance() or QApplication([])
    QSettings().clear()
    yield application
    QSettings().clear()


def test_anonymize():
    text = r"C:\Users\Erika\Musik\a.mp3 und C:/Users/Erika/x, 192.168.178.124, Karte 096195156048 / 096 195 156 048"
    out = fb.anonymize(text, home=r"C:\Users\Erika")
    assert "Erika" not in out and r"~\Musik" in out and "~/x" in out
    assert "192.168" not in out and "x.x.x.x" in out
    assert "096" not in out and out.count("<Kartennummer>") == 2


def test_report_and_urls_follow_mode(app):
    app_mode.set_mode(app_mode.MODE_ESPUINO)
    try:
        text = fb.report_text(firmware="dev 20261008", log="Zeile 1\nZeile 2\n")
        assert __version__ in text and "Modus: ESPuino" in text and "dev 20261008" in text
        assert text.rstrip().endswith("Zeile 2")
        assert fb.forum_url() == fb.FEEDBACK_ESPUINO_URL
        query = parse_qs(urlparse(fb.issue_url("dev 20261008")).query)
        assert "Modus: ESPuino" in query["body"][0] and "Zeile" not in query["body"][0]
    finally:
        app_mode.set_mode(app_mode.MODE_TONUINO)
    assert fb.forum_url() == fb.FEEDBACK_TONUINO_URL
    dialog = fb.FeedbackDialog(firmware="")
    assert "Modus: TonUINO" in dialog.text_edit.toPlainText()
    dialog.copy()
    assert QApplication.clipboard().text() == dialog.text_edit.toPlainText()
    dialog.deleteLater()
