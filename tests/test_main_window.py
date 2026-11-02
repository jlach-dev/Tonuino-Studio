import pytest

pytest.importorskip("PyQt6")

from PyQt6.QtCore import QCoreApplication, QSettings
from PyQt6.QtWidgets import QApplication, QDialog


@pytest.fixture(scope="module")
def app():
    QCoreApplication.setOrganizationName("Tonuino-Manager-Tests")
    QCoreApplication.setApplicationName("tests-main")
    application = QApplication.instance() or QApplication([])
    QSettings().clear()
    yield application
    QSettings().clear()


def test_main_window_full_screen_and_state(app):
    from gui.main_window import MainWindow

    window = MainWindow()
    window.show()
    app.processEvents()
    assert window.minimumSize().width() <= 900
    assert window.status_bar.isVisible()  # sonst sieht niemand Fehlermeldungen

    window.toggle_full_screen()
    app.processEvents()
    assert window.isFullScreen() and window.action_full_screen.isChecked()
    window._leave_full_screen()
    app.processEvents()
    assert not window.isFullScreen()

    window.toggle_maximized()
    app.processEvents()
    assert window.isMaximized()

    # offener Dialog wird mit dem Hauptfenster geschlossen
    dialog = QDialog(window)
    dialog.show()
    assert dialog.isVisible()
    window.close()
    app.processEvents()
    assert not dialog.isVisible()
    assert QSettings().value("window/maximized", type=bool)
    assert QSettings().value("window/splitter") is not None
