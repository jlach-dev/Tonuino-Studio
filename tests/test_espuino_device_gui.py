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
    yield {"esp": fake, "sd": sd, "data": tmp_path / "studio-data"}
    settings.clear()
    fake.close()


def test_source_defaults(app):
    QSettings().clear()
    assert dev.settings()[0] == dev.SOURCE_DEVICE
    QSettings().setValue("mediahub/db_path", "x/db.json")  # wer schon einen Mediahub hat, behaelt ihn
    assert dev.settings()[0] == dev.SOURCE_MEDIAHUB
    QSettings().clear()


def test_window_shows_device_and_known_cards(setup):
    from core.espuino_cards import ASSIGNED
    from gui.main_window import MainWindow
    window = MainWindow()
    try:
        assert window.view_switch.tabText(0) == "ESPuino-Karten"
        side, page = window.espuino_sidebar, window.mediahub_page
        assert window.mediahub_sidebar.isHidden() and not side.isHidden()
        assert side.btn_assign.isEnabled() and "31,0 GB frei" in side.device_label.text()
        assert [side.folder_list.item(i).text() for i in range(side.folder_list.count())] == ["Hörspiele", "Musik"]  # SD im Kartenleser
        assert side.folder_list.drop_enabled
        table = page.cards_table.tree
        assert table.topLevelItemCount() == 2 and page.pages.currentIndex() == 2
        text = page.device_text.text()
        assert "2 bekannte Karten" in text and "1 Funktionskarte" in text and "1 vom Mediahub verwaltet" in text
        window._mediahub_show_card(CARD_A)
        assert page.card.card_id == CARD_A and page.card.device_status == ASSIGNED
        assert "SD-Karte: /Hörspiele/Fuchsbande" in page.meta_label.text()
        assert page.file_list.count() == 2 and page.btn_cover.isEnabled()  # Dateien von der SD im Kartenleser
        assert not page.btn_back.isHidden() and not page.btn_transfer.isHidden()
        assert not page.btn_transfer.isEnabled()  # nicht in der Bibliothek
        page.btn_back.click()
        assert page.pages.currentIndex() == 2
        # das Verzeichnis wurde im Datenordner gespeichert
        assert (setup["data"] / "espuino" / "cards.json").is_file()
    finally:
        window.close()
        window.deleteLater()


def test_unreachable_device_keeps_known_cards(setup):
    from gui.main_window import MainWindow
    window = MainWindow()  # einmal erreichbar: Karten werden bekannt
    window.close()
    window.deleteLater()
    dev.save_settings(dev.SOURCE_DEVICE, "127.0.0.1:9", str(setup["sd"]))
    window = MainWindow()
    try:
        assert window.mediahub_page.cards_table.tree.topLevelItemCount() == 2
        assert "nicht erreichbar" in window.mediahub_page.device_text.text()
        assert "nicht erreichbar" in window.espuino_sidebar.device_label.text()
        assert not window.espuino_sidebar.btn_assign.isEnabled()
    finally:
        window.close()
        window.deleteLater()


def test_transfer_known_card_uploads_and_assigns(setup, tmp_path):
    from core.espuino_cards import NOT_ASSIGNED
    from core.library import LibraryAlbum
    from gui.espuino_device_view import needs_transfer
    from gui.espuino_dialogs import TransferWorker
    from core.espuino_transfer import plan_album
    esp = setup["esp"]
    folder = tmp_path / "Bibliothek" / "Die Fuchsbande"
    folder.mkdir(parents=True)
    tracks = []
    for name in ("01.mp3", "02.mp3"):
        (folder / name).write_bytes(b"ID3" + b"\0" * 64)
        tracks.append(str(folder / name))
    dev.save_settings(dev.SOURCE_DEVICE, esp.host, "")  # SD-Karte steckt im ESPuino
    from gui.main_window import MainWindow
    window = MainWindow()
    try:
        window.library_view.index.albums.append(LibraryAlbum(path=str(folder), title="Die Fuchsbande",
                                                             tracks=tracks, album_id="fuchs"))
        known = window._espuino_known
        known.remember("777888999000", name="Fuchsbande", sd_folder="/Die Fuchsbande", play_mode=5)
        known.save()
        window._mediahub_reload()
        card = window._mediahub_card("777888999000")
        assert card.device_status == NOT_ASSIGNED and card.folder_present is False and needs_transfer(card)
        assert card.library_path == str(folder) and window.mediahub_page.cards_table.btn_transfer_missing.isEnabled()
        worker = TransferWorker([plan_album(tracks, card.sd_path)], "", esp.host,
                                assignments=[(card.card_id, 5)])
        done = []
        worker.done.connect(lambda *a: done.append(a))
        worker.run()
        assert done == [(2, 0, False)] and "/Die Fuchsbande/02.mp3" in esp.files
        assert esp.assignments["777888999000"]["fileOrUrl"] == "/Die Fuchsbande"
        window._mediahub_reload()
        card = window._mediahub_card("777888999000")
        assert card.folder_present is True and not needs_transfer(card)
        assert window._espuino_known.get("777888999000").album_id == "fuchs"
    finally:
        window.close()
        window.deleteLater()


def test_device_folder_list_drop_rules(setup):
    from gui.espuino_device_view import DeviceFolderList
    widget = DeviceFolderList()
    widget.drop_enabled = True
    widget.album_names = lambda paths: ["Hörspiele"]
    widget.addItem("Hörspiele")
    widget.item(0).setData(0x0100, "/Hörspiele")
    assert widget.album_names(["x"]) == ["Hörspiele"]
    widget.deleteLater()


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


def test_cover_of_library_card_opens_the_library_album(setup, tmp_path, monkeypatch):
    from core.library import LibraryAlbum
    from gui.cover_manager_dialog import CoverManagerDialog
    from gui.main_window import MainWindow
    folder = tmp_path / "Bibliothek" / "Die Fuchsbande"
    folder.mkdir(parents=True)
    (folder / "01.mp3").write_bytes(b"ID3" + b"\0" * 64)
    window = MainWindow()
    try:
        window.library_view.index.albums.append(LibraryAlbum(path=str(folder), title="Die Fuchsbande",
                                                             tracks=[str(folder / "01.mp3")], album_id="fuchs"))
        window._espuino_known.remember("777888999000", name="Fuchsbande", sd_folder="/Die Fuchsbande",
                                       play_mode=5)
        window._espuino_known.save()  # Neu einlesen laedt das Verzeichnis von der Platte
        window._mediahub_reload()
        card = window._mediahub_card("777888999000")
        assert card.album_id == "fuchs"
        opened = []
        monkeypatch.setattr(window, "_library_cover", lambda album, focus=None: opened.append((album.album_id, focus)))
        window._mediahub_open_cover("777888999000")
        assert opened == [("fuchs", CoverManagerDialog.FOCUS_CARD)]
    finally:
        window.close()
        window.deleteLater()


def test_read_cards_window_shows_each_card(setup):
    """"Karte auslesen": jede aufgelegte Karte zeigt Name, Zuweisung und Kartencover"""
    from PIL import Image
    from PyQt6.QtCore import QEvent
    from gui.cover_manager_dialog import get_archive
    from gui.main_window import MainWindow
    window = MainWindow()
    try:
        assert window.espuino_sidebar.btn_read.isEnabled()
        card = window._mediahub_card(CARD_A)
        card_dir = get_archive(window, ask=False).card_dir(card.album_id)
        card_dir.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (101, 64), (0, 0, 200)).save(card_dir / "card.png")

        window._espuino_read_cards()
        reader = window._espuino_reader
        window._espuino_read_cards()  # zweiter Klick holt dasselbe Fenster nach vorn
        assert window._espuino_reader is reader
        reader.on_message('{"rfidId": "%s"}' % CARD_A)
        assert "001" in reader.number_label.text() and reader.info.known
        assert "/Hörspiele/Fuchsbande" in reader.info_label.text()
        assert reader.info.front_path.endswith("card.png") and not reader.front_box.image.pixmap().isNull()
        assert reader.btn_show.isEnabled() and not reader.btn_assign.isEnabled()

        reader.on_message('{"rfidId": "999999999999"}')
        assert not reader.info.known and "Unbekannte Karte" in reader.name_label.text()
        assert "nicht zugewiesen" in reader.info_label.text() and reader.btn_assign.isEnabled()
        reader.on_message('{"status": "ok"}')  # andere Meldungen des ESPuino aendern nichts
        assert reader.count == 2

        reader.close()
        QApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete.value)
        assert window._espuino_reader is None
    finally:
        window.close()
        window.deleteLater()


def test_click_on_device_folder_shows_its_cards(setup):
    """Klick auf einen Ordner der Seitenleiste: Infos und alle Karten, die darauf zeigen"""
    from gui.main_window import MainWindow
    window = MainWindow()
    try:
        side, page = window.espuino_sidebar, window.mediahub_page
        item = next(side.folder_list.item(i) for i in range(side.folder_list.count())
                    if side.folder_list.item(i).text() == "Hörspiele")
        side.folder_list.itemClicked.emit(item)
        folder_page = page.folder_page
        assert page.pages.currentWidget() is folder_page and folder_page.folder == "/Hörspiele"
        assert folder_page.tree.topLevelItemCount() == 1 and folder_page.empty_label.isHidden()
        row = folder_page.tree.topLevelItem(0)
        assert row.data(0, 0x0100) == CARD_A and row.text(2) == "zugewiesen: /Hörspiele/Fuchsbande"
        assert "Kein passendes Album" in folder_page.meta_label.text() and folder_page.btn_assign.isEnabled()
        folder_page.tree.itemClicked.emit(row, 0)  # Karte oeffnen
        assert page.card is not None and page.card.card_id == CARD_A
        musik = next(side.folder_list.item(i) for i in range(side.folder_list.count())
                     if side.folder_list.item(i).text() == "Musik")
        side.folder_list.itemClicked.emit(musik)
        assert folder_page.tree.topLevelItemCount() == 1  # Karte B zeigt auf eine Datei darin
        folder_page.back_requested.emit()
        assert page.pages.currentIndex() == 2
    finally:
        window.close()
        window.deleteLater()


def test_card_info_and_folder_page_show_the_library_album(setup, tmp_path, monkeypatch):
    """Karte mit Album in der Bibliothek: Album und Vorschaubild (frueher AttributeError)"""
    from PIL import Image
    from core.library import LibraryAlbum
    from gui.main_window import MainWindow
    thumb = tmp_path / "thumb.png"
    Image.new("RGB", (40, 40), (200, 0, 0)).save(thumb)
    album = LibraryAlbum(path=str(tmp_path / "Fuchsbande"), title="Die Fuchsbande", tracks=["a.mp3", "b.mp3"],
                         duration=600.0, thumb="thumb.png")
    window = MainWindow()
    try:
        card = window._mediahub_card(CARD_A)
        monkeypatch.setattr(card, "library_path", album.path)
        monkeypatch.setattr(window.library_view.index, "find", lambda path: album if path == album.path else None)
        monkeypatch.setattr(window.library_view, "library", type("Lib", (), {"thumb_path": lambda self, a: thumb})())
        info = window._espuino_card_info(CARD_A)
        assert info.album == "Die Fuchsbande" and info.album_detail == "2 Titel, 10 min"
        assert info.album_cover_path == str(thumb)
        window._espuino_show_folder("/Hörspiele/Fuchsbande")
        assert "Album: Die Fuchsbande" in window.mediahub_page.folder_page.meta_label.text()
        monkeypatch.setattr(window.library_view, "library", None)  # Bibliothek noch nicht geladen
        assert window._espuino_card_info(CARD_A).album_cover_path == ""
    finally:
        window.close()
        window.deleteLater()
