"""ESPuino-Modus mit Karten direkt vom Geraet: Hauptfenster und Zuweisen
gegen einen nachgebauten ESPuino (erfundene Daten)"""

import pytest

pytest.importorskip("PyQt6")

from PyQt6.QtCore import QCoreApplication, QSettings
from PyQt6.QtWidgets import QApplication

from core import espuino_device as dev
from core.app_mode import MODE_ESPUINO, SETTINGS_APP_MODE
from tests.test_espuino_device import CARD_A, CARD_B, FakeEspuino


@pytest.fixture(scope="module")
def app():
    QCoreApplication.setOrganizationName("Tonuino-Manager-Tests")
    QCoreApplication.setApplicationName("tests-espuino-device")
    application = QApplication.instance() or QApplication([])
    QSettings().clear()
    yield application
    QSettings().clear()


@pytest.fixture
def setup(tmp_path, app):
    fake = FakeEspuino()
    sd = tmp_path / "sd"
    for rel in ("Hörspiele/Fuchsbande/01.mp3", "Hörspiele/Fuchsbande/02.mp3", "Musik/Lieder/02.mp3",
                "Hörspiele/Neu/01.mp3"):
        path = sd / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"ID3" + b"\0" * 32)
    settings = QSettings()
    settings.clear()
    settings.setValue(SETTINGS_APP_MODE, MODE_ESPUINO)
    settings.setValue("library/data_dir", str(tmp_path / "studio-data"))
    dev.save_settings(dev.SOURCE_DEVICE, fake.host, str(sd))
    yield {"esp": fake, "sd": sd}
    settings.clear()
    fake.close()


def test_source_defaults(app):
    QSettings().clear()
    assert dev.settings()[0] == dev.SOURCE_DEVICE
    QSettings().setValue("mediahub/db_path", "x/db.json")  # wer schon einen Mediahub hat, behaelt ihn
    assert dev.settings()[0] == dev.SOURCE_MEDIAHUB
    QSettings().clear()


def test_window_shows_cards_from_device(setup):
    from gui.main_window import MainWindow
    window = MainWindow()
    try:
        assert window.view_switch.tabText(0) == "ESPuino-Karten"
        sidebar, page = window.mediahub_sidebar, window.mediahub_page
        assert not sidebar.btn_assign.isHidden() and sidebar.btn_assign.isEnabled()
        assert sidebar.card_list.count() == 2
        text = page.overview_text.text()
        assert "2 Karten vom ESPuino" in text and "1 Funktionskarte" in text and "1 vom Mediahub verwaltet" in text
        sidebar.select(CARD_A)
        assert page.card.card_id == CARD_A
        assert "SD-Karte: /Hörspiele/Fuchsbande" in page.meta_label.text()
        assert page.file_list.count() == 2 and page.btn_cover.isEnabled()
    finally:
        window.close()
        window.deleteLater()


def test_unreachable_device_shows_message(setup):
    from gui.main_window import MainWindow
    dev.save_settings(dev.SOURCE_DEVICE, "127.0.0.1:9", str(setup["sd"]))
    window = MainWindow()
    try:
        assert window.mediahub_sidebar.card_list.count() == 0
        assert "nicht erreichbar" in window.mediahub_page.overview_text.text()
    finally:
        window.close()
        window.deleteLater()


def test_assign_dialog_takes_card_from_websocket_and_saves(setup):
    from gui.espuino_dialogs import AssignCardDialog
    esp = setup["esp"]
    dialog = AssignCardDialog(esp.host, str(setup["sd"]), {CARD_B: "02.mp3"}, listen=False)
    try:
        dialog.on_message('{"opmode":0}')
        assert dialog.card_id == ""
        dialog.on_message('{"rfidId":"%s"}' % CARD_B)
        assert dialog.card_id == CARD_B and not dialog.card_hint.isHidden()  # schon belegt
        dialog.on_message('{"rfidId":"555666777888"}')
        assert dialog.card_id == "555666777888" and dialog.card_hint.isHidden()
        dialog.path_edit.setText("Hörspiele/Gibt es nicht")
        assert not dialog.path_hint.isHidden()  # Ordner fehlt auf der SD-Karte
        dialog.path_edit.setText("Hörspiele/Neu")
        assert dialog.sd_path == "/Hörspiele/Neu" and dialog.path_hint.isHidden()
        dialog.mode_combo.setCurrentIndex(dialog.mode_combo.findData(5))
        dialog.try_accept()
        assert esp.posts == [{"id": "555666777888", "fileOrUrl": "/Hörspiele/Neu", "playMode": 5}]
        assert dialog.result_entry["id"] == "555666777888"
    finally:
        dialog.deleteLater()


def test_setup_dialog_checks_address(setup):
    from gui.espuino_dialogs import EspuinoSetupDialog
    esp = setup["esp"]
    dialog = EspuinoSetupDialog(dev.SOURCE_DEVICE, "http://" + esp.host + "/", str(setup["sd"]))
    try:
        assert dialog.hub_box.isEnabled() is False
        assert dialog.check_device()
        assert dialog.host == esp.host and "20261008-DEV" in dialog.test_label.text()
        dialog.host_edit.setText("127.0.0.1:9")
        assert not dialog.check_device()
        dialog.radio_hub.setChecked(True)
        assert dialog.source == dev.SOURCE_MEDIAHUB and not dialog.device_box.isEnabled()
    finally:
        dialog.deleteLater()


def test_transfer_dialog_and_worker_upload(setup, tmp_path):
    from core.library import LibraryAlbum
    from gui.espuino_dialogs import TransferDialog, TransferWorker
    from tests.test_espuino_transfer import FakeExplorer
    folder = tmp_path / "Bibliothek" / "Fuchsbande 1"
    folder.mkdir(parents=True)
    tracks = []
    for name in ("01.mp3", "02.mp3"):
        (folder / name).write_bytes(b"ID3" + b"\0" * 64)
        tracks.append(str(folder / name))
    album = LibraryAlbum(path=str(folder), title="Die Fuchsbande: Folge 1", tracks=tracks)
    fake = FakeExplorer()
    try:
        dialog = TransferDialog([album], "", fake.host)  # keine SD-Karte im PC: ueber das WLAN
        assert dialog.folder_edit.text() == "/Die Fuchsbande Folge 1"
        assert dialog.assign_after
        dialog.try_accept()
        assert [i.target for i in dialog.plans[0].items] == ["/Die Fuchsbande Folge 1/01.mp3",
                                                            "/Die Fuchsbande Folge 1/02.mp3"]
        worker = TransferWorker(dialog.plans, "", fake.host)
        results = []
        worker.done.connect(lambda *args: results.append(args))
        worker.run()
        assert results == [(2, 0, False)]
        assert "/Die Fuchsbande Folge 1/02.mp3" in fake.files
        dialog.deleteLater()
    finally:
        fake.close()


def test_transfer_menu_in_device_mode(setup):
    from gui.main_window import MainWindow
    window = MainWindow()
    try:
        assert window.library_view.transfer_label == "Auf den ESPuino übertragen"
        assert window.library_view.transfer_enabled()
    finally:
        window.close()
        window.deleteLater()
