"""Mehrere ESPuinos und der Dialog "Mit ESPuino verbinden" (nachgebauter ESPuino,
erfundene Adressen aus dem Dokumentationsnetz 192.0.2.0/24)"""

import time

import pytest

pytest.importorskip("PyQt6")

from PyQt6.QtCore import QCoreApplication, QSettings
from PyQt6.QtWidgets import QApplication, QDialog

from core import espuino_device as dev
from core.app_mode import MODE_ESPUINO, SETTINGS_APP_MODE
from tests.test_espuino_device import CARD_A, FakeEspuino

OFFLINE = "127.0.0.1:9"  # hier antwortet niemand


@pytest.fixture(scope="module")
def app():
    QCoreApplication.setOrganizationName("Tonuino-Manager-Tests")
    QCoreApplication.setApplicationName("tests-espuino-connect")
    application = QApplication.instance() or QApplication([])
    QSettings().clear()
    yield application
    QSettings().clear()


@pytest.fixture
def esp(app):
    QSettings().clear()
    fake = FakeEspuino()
    yield fake
    fake.close()
    QSettings().clear()


def wait_for(condition, timeout=8.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        QApplication.processEvents()
        if condition():
            return True
        time.sleep(0.02)
    return False


def test_device_list_takes_over_the_old_single_address(app):
    QSettings().clear()
    QSettings().setValue(dev.SETTINGS_HOST, "192.0.2.10")
    QSettings().setValue(dev.SETTINGS_SD_ROOT, "E:/")
    assert [(d.host, d.sd_root) for d in dev.saved_devices()] == [("192.0.2.10", "E:/")]
    dev.remember_device("192.0.2.11", name="ESPuino Bad")
    dev.remember_device("192.0.2.10", name="ESPuino Kinderzimmer")
    assert [d.label for d in dev.saved_devices()] == ["ESPuino Kinderzimmer", "ESPuino Bad"]
    assert dev.find_device("192.0.2.10").sd_root == "E:/"  # bleibt beim Umbenennen

    dev.set_current("192.0.2.11")
    assert dev.settings()[1:] == ("192.0.2.11", "")
    dev.save_settings(dev.SOURCE_DEVICE, "192.0.2.12", "F:/")  # Einrichten legt das Geraet an
    assert dev.find_device("192.0.2.12").sd_root == "F:/" and dev.current_host() == "192.0.2.12"

    dev.forget_device("192.0.2.12")
    assert dev.current_host() == "" and dev.find_device("192.0.2.12") is None
    assert len(dev.saved_devices()) == 2
    QSettings().clear()


def test_probe_reads_ip_and_hostname(esp):
    result = dev.probe(esp.host)
    assert result.reachable and result.ip == "192.0.2.24" and result.hostname == "ESPuino-Testzimmer"
    assert "20261008-DEV" in result.firmware
    offline = dev.probe(OFFLINE, timeout=1)
    assert not offline.reachable and "nicht erreichbar" in offline.error


def test_state_without_connection_does_not_ask_the_device(esp, tmp_path):
    state = dev.fetch_state(esp.host, str(tmp_path), connect=False)
    assert not state.reachable and state.error == dev.NOT_CONNECTED and esp.requests == []
    assert state.folders == [] and state.folders_from_pc


def test_connect_dialog_lists_devices_and_connects(esp):
    from gui.espuino_connect_dialog import ConnectDialog
    dev.remember_device(OFFLINE, name="ESPuino Keller")
    dev.remember_device(esp.host)  # ohne Namen: Hostname des ESPuino wird uebernommen
    dialog = ConnectDialog()
    try:
        assert dialog.tree.topLevelItemCount() == 2
        assert wait_for(lambda: esp.host in dialog._results and OFFLINE in dialog._results)
        online, offline = dialog._item(esp.host), dialog._item(OFFLINE)
        assert online.text(0) == "ESPuino-Testzimmer" and dev.find_device(esp.host).name == "ESPuino-Testzimmer"
        assert online.text(2).startswith("verbunden") and "192.0.2.24" in online.text(2)
        assert offline.text(2) == "nicht erreichbar"
        dialog.tree.setCurrentItem(offline)
        assert not dialog.btn_connect.isEnabled()
        dialog.tree.setCurrentItem(online)
        assert dialog.btn_connect.isEnabled()
        dialog.btn_connect.click()
        assert dialog.result() == QDialog.DialogCode.Accepted and dialog.selected_host == esp.host
    finally:
        dialog.deleteLater()


def test_connect_dialog_connects_by_itself_when_not_asking(esp):
    from gui.espuino_connect_dialog import ConnectDialog
    dev.remember_device(esp.host)
    dev.set_current(esp.host)
    dialog = ConnectDialog(auto_connect=True)
    try:
        assert wait_for(lambda: dialog.result() == QDialog.DialogCode.Accepted)
        assert dialog.selected_host == esp.host
    finally:
        dialog.deleteLater()


@pytest.fixture
def window_setup(esp, tmp_path):
    from gui.main_window import MainWindow
    settings = QSettings()
    settings.setValue(SETTINGS_APP_MODE, MODE_ESPUINO)
    settings.setValue("library/data_dir", str(tmp_path / "studio-data"))
    dev.save_settings(dev.SOURCE_DEVICE, esp.host, "", name="ESPuino Kinderzimmer")
    MainWindow.espuino_ask_on_start = True
    yield esp
    MainWindow.espuino_ask_on_start = False


def test_start_waits_for_the_choice_and_shows_cards_offline(window_setup, monkeypatch):
    from gui import espuino_connect_dialog
    from gui.main_window import MainWindow
    esp = window_setup
    window = MainWindow()
    try:
        side = window.espuino_sidebar
        assert esp.requests == []  # der Start fragt nicht im WLAN
        assert "ESPuino Kinderzimmer" in side.device_label.text() and "nicht verbunden" in side.device_label.text()
        assert side.btn_connect.text() == "Verbinden …" and side.btn_assign.isEnabled()  # auslesen per ACR122U
        assert not window._espuino_can_transfer()

        class Choose:  # Dialog ersetzt: Nutzer waehlt den ESPuino
            def __init__(self, *_args, **kwargs):
                self.selected_host = esp.host
                self.auto_connect = kwargs.get("auto_connect")

            def exec(self):
                return QDialog.DialogCode.Accepted

            def deleteLater(self):
                pass

        monkeypatch.setattr(espuino_connect_dialog, "ConnectDialog", Choose)
        window.espuino_start_session()
        assert "/rfid" in esp.requests and side.btn_assign.isEnabled()
        assert side.btn_connect.text() == "ESPuino wechseln …"
        assert "192.0.2.24" in side.device_label.text()
        assert window._mediahub_data is not None and any(c.card_id == CARD_A for c in window._mediahub_data.cards)
        esp.requests.clear()
        window.espuino_start_session()  # schon verbunden: kein zweites Fenster
        assert esp.requests == []
    finally:
        window.close()
        window.deleteLater()


def test_start_without_device_keeps_known_cards(window_setup, monkeypatch):
    from gui import espuino_connect_dialog
    from gui.main_window import MainWindow
    esp = window_setup

    class Cancel:
        def __init__(self, *_args, **_kwargs):
            self.selected_host = ""

        def exec(self):
            return QDialog.DialogCode.Rejected

        def deleteLater(self):
            pass

    monkeypatch.setattr(espuino_connect_dialog, "ConnectDialog", Cancel)
    window = MainWindow()
    try:
        window.espuino_start_session()
        assert esp.requests == [] and not window._espuino_connected
        assert window.espuino_sidebar.btn_connect.text() == "Verbinden …"
    finally:
        window.close()
        window.deleteLater()
