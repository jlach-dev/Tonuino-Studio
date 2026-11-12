"""ESPuino-Modus mit Karten direkt vom Geraet: Hauptfenster und Zuweisen
gegen einen nachgebauten ESPuino (erfundene Daten)"""

import pytest

pytest.importorskip("PyQt6")

from PyQt6.QtCore import QCoreApplication, QSettings
from PyQt6.QtWidgets import QApplication

from core import espuino_device as dev
from core.app_mode import MODE_ESPUINO, SETTINGS_APP_MODE
from tests.test_espuino_device import CARD_A, FakeEspuino


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
        assert table.headerItem().text(4) == "SD-Karte" and table.headerItem().text(5) == "Bibliothek"
        assert {table.topLevelItem(i).text(5) for i in range(2)} == {"fehlt"}  # kein Album in der Bibliothek
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
        assert window.espuino_sidebar.btn_assign.isEnabled()  # auslesen geht (ACR122U), zuweisen nicht
        reader = window._espuino_read_cards()
        assert reader.btn_assign.text() == "Vormerken" and "Vormerken" in reader.assign_note.text()
        reader.close()
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


def test_assign_in_the_read_window(setup, monkeypatch):
    """Karte auflegen, Ordner aus der Liste der ESPuino-Ordner, zuweisen - im selben Fenster"""
    from core import espuino_cards
    from gui.main_window import MainWindow
    from PyQt6.QtWidgets import QMessageBox
    esp = setup["esp"]
    window = MainWindow()
    try:
        reader = window._espuino_read_cards()
        assert reader.assign_box.isEnabled()
        choices = reader.folder_choices
        assert "/Hörspiele" in choices and "/Hörspiele/Fuchsbande" in choices  # Geraet und bekannte Karten
        reader.on_message('{"opmode":0}')
        assert reader.card_id == ""
        reader.on_message('{"rfidId":"%s"}' % CARD_A)  # bekannt: vorbelegt, "Neu zuweisen"
        assert reader.sd_path == "/Hörspiele/Fuchsbande" and reader.btn_assign.text() == "Neu zuweisen"
        reader.on_message('{"rfidId":"555666777888"}')
        assert reader.card_id == "555666777888" and reader.btn_assign.text() == "Zuweisen" and reader.sd_path == ""
        reader._set_path("Hörspiele/Gibt es nicht")
        assert not reader.path_hint.isHidden()  # Ordner fehlt auf der SD-Karte im Kartenleser
        reader._set_path("/Hörspiele/Neu")
        assert reader.path_hint.isHidden()
        reader.mode_combo.setCurrentIndex(reader.mode_combo.findData(5))
        assert reader.try_assign()
        assert esp.posts == [{"id": "555666777888", "fileOrUrl": "/Hörspiele/Neu", "playMode": 5}]
        known = espuino_cards.load(str(setup["data"]))
        assert known.get("555666777888").sd_folder == "/Hörspiele/Neu"  # im Verzeichnis gemerkt
        assert reader.info.assignment == "/Hörspiele/Neu" and reader.btn_assign.text() == "Neu zuweisen"

        # andere Karte auf einen anderen Ordner: Rueckfrage, Nein laesst alles, wie es ist
        monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.No)
        reader.on_message('{"rfidId":"%s"}' % CARD_A)
        reader._set_path("/Hörspiele/Neu")
        assert not reader.try_assign() and len(esp.posts) == 1

        # vorgegebener Ordner (Ordnerseite, zweites Kartenset) bleibt fuer die naechste Karte stehen
        window._espuino_assign(preset_path="/Hörspiele/Neu")
        assert window._espuino_reader is reader and reader.sd_path == "/Hörspiele/Neu"
        reader.on_message('{"rfidId":"999888777666"}')
        assert reader.sd_path == "/Hörspiele/Neu" and reader.try_assign()
        assert reader.sd_path == "/Hörspiele/Neu" and len(esp.posts) == 2
        reader.close()
    finally:
        window.close()
        window.deleteLater()


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
        assert window.espuino_sidebar.btn_assign.isEnabled()
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
        assert reader.btn_show.isEnabled() and reader.btn_assign.text() == "Neu zuweisen"

        reader.on_message('{"rfidId": "999999999999"}')
        assert not reader.info.known and "Unbekannte Karte" in reader.name_label.text()
        assert "nicht zugewiesen" in reader.info_label.text() and reader.btn_assign.text() == "Zuweisen"
        reader.on_message('{"status": "ok"}')  # andere Meldungen des ESPuino aendern nichts
        assert reader.count == 2

        reader.close()
        QApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete.value)
        assert window._espuino_reader is None
    finally:
        window.close()
        window.deleteLater()


def test_click_on_device_folder_shows_card_and_folder_in_one_view(setup):
    """Klick auf einen Ordner: Kartenansicht mit allen Karten des Ordners und den Dateien;
    Ordner ohne Karte: Ordnerseite mit Album bzw. Dateien. Filter in der Ordnerliste."""
    from gui.main_window import MainWindow
    window = MainWindow()
    try:
        side, page = window.espuino_sidebar, window.mediahub_page
        item = next(side.folder_list.item(i) for i in range(side.folder_list.count())
                    if side.folder_list.item(i).text() == "Hörspiele")
        side.folder_list.itemClicked.emit(item)
        assert page.pages.currentIndex() == 1 and page.card.card_id == CARD_A
        assert not page.siblings_box.isHidden() and page.siblings_tree.topLevelItemCount() == 1
        row = page.siblings_tree.topLevelItem(0)
        assert row.text(2) == "zugewiesen" and row.font(0).bold()  # gezeigte Karte hervorgehoben
        assert page.file_list.count() == 2 and page.btn_assign_more.isEnabled()

        window._espuino_show_folder("/Leer")  # Ordner ohne Karte
        folder_page = page.folder_page
        assert page.pages.currentWidget() is folder_page and folder_page.tree.isHidden()
        assert not folder_page.empty_label.isHidden() and folder_page.file_list.count() == 1
        assert not folder_page.btn_cover.isEnabled()  # weder Album noch Karte
        folder_page.back_requested.emit()
        assert page.pages.currentIndex() == 2

        side.folder_filter.setText("musik")
        visible = [side.folder_list.item(i).text() for i in range(side.folder_list.count())
                   if not side.folder_list.item(i).isHidden()]
        assert visible == ["Musik"]
        side.folder_filter.clear()
        assert all(not side.folder_list.item(i).isHidden() for i in range(side.folder_list.count()))
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
        window._espuino_show_folder("/Hörspiele/Fuchsbande")  # Ordner mit Karte: Kartenansicht
        assert window.mediahub_page.card.card_id == CARD_A
        monkeypatch.setattr(window.library_view, "library", None)  # Bibliothek noch nicht geladen
        assert window._espuino_card_info(CARD_A).album_cover_path == ""
    finally:
        window.close()
        window.deleteLater()


def test_read_cards_with_acr122u_without_playing_on_the_espuino(setup):
    """Karte auf dem ACR122U: gleiche Anzeige, die UID wird zur ESPuino-Nummer"""
    from core.rfid import RfidStatus
    from gui.main_window import MainWindow
    dev.set_use_acr122u(True)
    window = MainWindow()
    try:
        assert not window.rfid_panel._rfid_poller.paused
        window._espuino_read_cards()
        reader = window._espuino_reader
        poller = window.rfid_panel._rfid_poller
        poller.status_changed.emit(RfidStatus(reader="ok"))
        assert "bereit" in reader.reader_label.text() and reader.count == 0
        on_card = RfidStatus(reader="ok", card="ok", uid="01020304")  # = 001 002 003 004 (CARD_A)
        poller.status_changed.emit(on_card)
        assert reader.info.card_id == CARD_A and reader.info.known and "am ACR122U" in reader.number_label.text()
        poller.status_changed.emit(on_card)  # liegt weiter auf: nicht erneut melden
        assert reader.count == 1
        poller.status_changed.emit(RfidStatus(reader="ok"))  # abgenommen und wieder aufgelegt
        poller.status_changed.emit(on_card)
        assert reader.count == 2
        poller.status_changed.emit(RfidStatus(reader="neutral"))
        assert "nicht angeschlossen" in reader.reader_label.text()
        reader.close()
    finally:
        window.close()
        window.deleteLater()


def test_folder_page_opens_the_cover_manager_of_the_album(setup, monkeypatch, tmp_path):
    from core.library import LibraryAlbum
    from gui.main_window import MainWindow
    album = LibraryAlbum(path=str(tmp_path / "Fuchsbande"), title="Die Fuchsbande", tracks=["a.mp3"])
    window = MainWindow()
    try:
        opened = []
        monkeypatch.setattr(window, "_espuino_album_for_folder", lambda folder: album)
        monkeypatch.setattr(window, "_library_cover", lambda a, focus=None: opened.append((a, focus)))
        window._espuino_show_folder("/Neues Album")  # Ordner ohne Karte
        folder_page = window.mediahub_page.folder_page
        assert "Album: Die Fuchsbande" in folder_page.meta_label.text() and folder_page.file_list.count() == 1
        assert folder_page.btn_cover.isEnabled()
        folder_page.btn_cover.click()
        assert opened and opened[0][0] is album
        assert window.mediahub_page.pages.currentWidget() is folder_page  # bleibt auf der Ordnerseite
    finally:
        window.close()
        window.deleteLater()



def test_acr122u_is_off_by_default_in_espuino_mode(setup):
    """Einstellung ACR122U (nur ESPuino-Modus, Standard aus): Leser wird nicht abgefragt"""
    from core.rfid import RfidStatus
    from gui.main_window import MainWindow
    assert not dev.use_acr122u()
    window = MainWindow()
    try:
        poller = window.rfid_panel._rfid_poller
        assert poller.paused and not window.action_use_acr122u.isChecked()
        reader = window._espuino_read_cards()
        assert "aus" in reader.reader_label.text()
        poller.status_changed.emit(RfidStatus(reader="ok", card="ok", uid="01020304"))
        assert reader.count == 0  # nicht verbunden
        window.action_use_acr122u.setChecked(True)  # sofort wirksam, auch im offenen Fenster
        assert dev.use_acr122u() and not poller.paused
        poller.status_changed.emit(RfidStatus(reader="ok", card="ok", uid="01020304"))
        assert reader.count == 1 and reader.info.card_id == CARD_A
        window.action_use_acr122u.setChecked(False)
        assert poller.paused and "aus" in reader.reader_label.text()
        reader.close()
    finally:
        window.close()
        window.deleteLater()


def test_assign_album_from_library_transfers_it_first(setup, tmp_path):
    """Album aus der Bibliothek waehlen, das auf dem ESPuino fehlt: Zuweisen ueberträgt erst, weist dann zu"""
    import time
    from core.library import LibraryAlbum
    from gui.main_window import MainWindow
    esp = setup["esp"]
    folder = tmp_path / "Bibliothek" / "Die Fuchsbande"
    folder.mkdir(parents=True)
    tracks = []
    for name in ("01.mp3", "02.mp3"):
        (folder / name).write_bytes(b"ID3" + b"\0" * 64)
        tracks.append(str(folder / name))
    dev.save_settings(dev.SOURCE_DEVICE, esp.host, "")  # SD-Karte steckt im ESPuino
    window = MainWindow()
    try:
        album = LibraryAlbum(path=str(folder), title="Die Fuchsbande", tracks=tracks, album_id="fuchs")
        window.library_view.index.albums.append(album)
        reader = window._espuino_read_cards()
        assert reader.albums == [album]
        reader.on_message('{"rfidId":"444555666777"}')
        reader.choose_album(album)
        assert reader.sd_path == "/Die Fuchsbande" and "fehlt auf dem ESPuino" in reader.path_hint.text()
        assert reader.try_assign()
        deadline = time.monotonic() + 10
        while "444555666777" not in esp.assignments and time.monotonic() < deadline:
            QApplication.processEvents()
            time.sleep(0.02)
        worker = window._espuino_transfer_worker
        if worker is not None:
            worker.wait(5000)
        QApplication.processEvents()
        assert "/Die Fuchsbande/02.mp3" in esp.files
        assert esp.assignments["444555666777"]["fileOrUrl"] == "/Die Fuchsbande"
        known = window._espuino_known.get("444555666777")
        assert known is not None and known.album_id == "fuchs"
        assert reader.info.assignment == "/Die Fuchsbande"
        reader.close()
    finally:
        window.close()
        window.deleteLater()


def test_open_window_follows_the_connection(setup):
    """Fenster offen, bevor der ESPuino verbunden war: nach dem Einlesen Ordner und Zuweisen da"""
    from gui.main_window import MainWindow
    window = MainWindow()
    try:
        window._espuino_connected = False
        window._mediahub_reload()
        reader = window._espuino_read_cards()
        assert reader.btn_assign.text() == "Vormerken"
        window._espuino_connected = True
        window._mediahub_reload()
        assert reader.host and reader.btn_assign.text() == "Zuweisen"
        choices = reader.folder_choices
        assert "/Hörspiele" in choices
        reader.close()
    finally:
        window.close()
        window.deleteLater()



def test_folder_picker_searches_names_artists_and_paths(app, tmp_path):
    """Waehlen: Suche ueber Name, Interpret und Pfad (\"bibi\" findet Bibi und Tina), Ordner und Alben"""
    from core.library import LibraryAlbum
    from gui.espuino_card_reader import CardReaderDialog, FolderPicker
    albums = [
        LibraryAlbum(path=str(tmp_path / "Bibi und Tina" / "Folge 023"), title="Folge 023: Ein Pferd für Tante Paula",
                     artist="Bibi und Tina", tracks=["a.mp3"]),
        LibraryAlbum(path=str(tmp_path / "Conni" / "Reiterhof"), title="Conni auf dem Reiterhof",
                     artist="Conni", tracks=["a.mp3", "b.mp3"]),
    ]
    on_device = {"/conni auf dem reiterhof"}
    picker = FolderPicker(["/Folge 023 Ein Pferd für Tante Paula", "/Musik"], albums,
                          lambda f: f.casefold() in on_device, CardReaderDialog.album_folder,
                          notes={"/musik": "Karte: Lieder"})
    try:
        def visible(group):
            return [group.child(i).text(0) for i in range(group.childCount()) if not group.child(i).isHidden()]
        picker.search.setText("bibi")
        assert visible(picker.library_group) == ["Folge 023: Ein Pferd für Tante Paula"]  # ueber den Interpreten
        picker.search.setText("tante pferd")  # alle Woerter, Reihenfolge egal
        assert visible(picker.device_group) == ["Folge 023 Ein Pferd für Tante Paula"]
        picker.search.setText("lieder")  # Kartenname am Ordner
        assert visible(picker.device_group) == ["Musik"] and visible(picker.library_group) == []
        picker.search.setText("reiterhof")
        item = next(picker.library_group.child(i) for i in range(2)
                    if picker.library_group.child(i).text(0) == "Conni auf dem Reiterhof")
        assert "auf dem ESPuino" in item.text(1) and picker.tree.currentItem() is item
        picker.btn_ok.click()
        assert picker.chosen_album is albums[1] and picker.chosen_folder == ""
    finally:
        picker.deleteLater()


def test_series_batch_and_rework_on_espuino_cards(setup, monkeypatch):
    """Serienstapel im ESPuino-Modus: Karten der Serie ohne Cover; abgelehnte lassen sich nacharbeiten"""
    from core import cover_generator, cover_research
    from core.cover_generator import CONTEXT_MOTIF, ImageRefused
    from gui.cover_manager_dialog import get_archive
    from gui.main_window import MainWindow
    from gui.workers import CoverBatchWorker
    window = MainWindow()
    try:
        assert window.action_series_cover_batch.isVisible() is not False
        assert "Karten einer Serie" in window.action_series_cover_batch.text()
        archive = get_archive(window, ask=False)
        profile = archive.series.create("Die Fuchsbande")
        card = window._mediahub_card(CARD_A)
        archive.ensure_album(card.album_id, "Fuchsbande")
        archive.assign_album(card.album_id, profile.id)
        folders = window._series_folders(profile, archive)
        assert [f.card_id for f in folders] == [CARD_A] and window._series_unit() == "Karten"

        # Google lehnt ab: der Worker meldet die Karte zum Nacharbeiten
        monkeypatch.setattr(cover_research, "gather_folder_info", lambda folder, *a, **k: object())
        monkeypatch.setattr(cover_research, "research", lambda *a, **k: object())

        def refuse(*_a, **_k):
            raise ImageRefused(CONTEXT_MOTIF)
        monkeypatch.setattr(cover_generator, "generate_art", refuse)
        monkeypatch.setattr(cover_generator, "GenerationRequest", lambda **k: type("R", (), {})())
        worker = CoverBatchWorker(folders, window.metadata_manager, object(), "t", "i", "ai", "Stil", "portrait")
        refused, errors = [], []
        worker.refused.connect(refused.append)
        worker.error.connect(lambda name, msg: errors.append(msg))
        worker.run()
        assert refused == folders and "abgelehnt" in errors[0]

        opened = []
        monkeypatch.setattr(window, "_mediahub_open_cover", lambda card_id="": opened.append(card_id))
        window._rework_refused(refused)
        assert opened == [CARD_A]
    finally:
        window.close()
        window.deleteLater()



def test_card_table_checkboxes_sorting_and_value_filters(setup, monkeypatch):
    """Checkboxen: nur angehakte uebertragen; Spaltenkopf: sortieren und nach Werten filtern"""
    from PyQt6.QtCore import Qt
    from gui.espuino_device_view import COL_CHECK, COL_NAME, COL_SD
    from gui.main_window import MainWindow
    window = MainWindow()
    try:
        table = window.mediahub_page.cards_table
        tree = table.tree
        assert not table.btn_transfer_selected.isEnabled()
        row = tree.topLevelItem(0)
        opened = []
        table.card_selected.connect(opened.append)
        tree.itemClicked.emit(row, COL_CHECK)  # Klick auf die Checkbox-Spalte oeffnet nichts
        assert opened == []
        row.setCheckState(COL_CHECK, Qt.CheckState.Checked)
        assert table.checked_ids() == [row.data(0, 0x0100)] and table.btn_transfer_selected.isEnabled()
        assert "(1)" in table.btn_transfer_selected.text()
        sent = []
        monkeypatch.setattr(window, "_espuino_transfer_cards", lambda cards: sent.append([c.card_id for c in cards]))
        table.btn_transfer_selected.click()
        assert sent == [[row.data(0, 0x0100)]]
        window._mediahub_reload()  # Auswahl bleibt beim Neueinlesen erhalten
        assert table.checked_ids() == sent[0]
        table._check_where(lambda _cid: False)
        assert table.checked_ids() == []
        table._check_where(lambda _cid: True)
        assert len(table.checked_ids()) == 2

        table._sort(COL_NAME, Qt.SortOrder.DescendingOrder)
        names = [tree.topLevelItem(i).text(COL_NAME) for i in range(tree.topLevelItemCount())]
        assert names == sorted(names, key=str.casefold, reverse=True)
        values = {tree.topLevelItem(i).text(COL_SD) for i in range(tree.topLevelItemCount())}
        hide = next(iter(values))
        table.set_value_filter(COL_SD, {hide})
        assert tree.headerItem().text(COL_SD).endswith("▾")
        assert all(tree.topLevelItem(i).isHidden() == (tree.topLevelItem(i).text(COL_SD) == hide)
                   for i in range(tree.topLevelItemCount()))
        table.set_value_filter(COL_SD, set())
        assert not any(tree.topLevelItem(i).isHidden() for i in range(tree.topLevelItemCount()))
    finally:
        window.close()
        window.deleteLater()


def test_sidebar_shows_and_filters_series(setup, monkeypatch):
    """Seitenleiste: Serie je Ordner (Interpret des Bibliotheksalbums bzw. Serienprofil) und Filter"""
    from core.library import LibraryAlbum
    from gui.main_window import MainWindow
    window = MainWindow()
    try:
        album = LibraryAlbum(path="X:/Musik", title="Lieder", artist="Kinderlieder", tracks=["a.mp3"])
        monkeypatch.setattr(window, "_espuino_album_for_folder",
                            lambda folder: album if folder.casefold() == "/musik" else None)
        window._mediahub_reload()
        side = window.espuino_sidebar
        texts = [side.folder_list.item(i).text() for i in range(side.folder_list.count())]
        assert "Musik   ·   Kinderlieder" in texts and "Hörspiele" in texts
        assert not side.series_combo.isHidden()
        side.series_combo.setCurrentIndex(side.series_combo.findData("Kinderlieder"))
        visible = [side.folder_list.item(i).text() for i in range(side.folder_list.count())
                   if not side.folder_list.item(i).isHidden()]
        assert visible == ["Musik   ·   Kinderlieder"]
        side.series_combo.setCurrentIndex(side.series_combo.findData(""))  # ohne Serie
        visible = [side.folder_list.item(i).text() for i in range(side.folder_list.count())
                   if not side.folder_list.item(i).isHidden()]
        assert visible == ["Hörspiele"]
    finally:
        window.close()
        window.deleteLater()


def test_sd_card_in_reader_works_while_espuino_is_off(setup, tmp_path, monkeypatch):
    """ESPuino aus, seine SD-Karte im PC: einrichten speichern, Ordner von der Karte, uebertragen ohne Zuweisen"""
    from PyQt6.QtWidgets import QMessageBox
    from core.library import LibraryAlbum
    from gui.espuino_dialogs import EspuinoSetupDialog
    from gui.main_window import MainWindow
    sd = setup["sd"]
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.Yes)
    dialog = EspuinoSetupDialog(dev.SOURCE_DEVICE, "127.0.0.1:9", str(sd))
    try:
        dialog.try_accept()  # nicht erreichbar: nach Rueckfrage trotzdem gespeichert
        assert dialog.result() == dialog.DialogCode.Accepted and not dialog.reachable
    finally:
        dialog.deleteLater()
    dev.save_settings(dev.SOURCE_DEVICE, "127.0.0.1:9", str(sd))

    folder = tmp_path / "Bibliothek" / "Die Fuchsbande"
    folder.mkdir(parents=True)
    tracks = []
    for name in ("01.mp3", "02.mp3"):
        (folder / name).write_bytes(b"ID3" + b"\0" * 64)
        tracks.append(str(folder / name))
    window = MainWindow()
    try:
        side, table = window.espuino_sidebar, window.mediahub_page.cards_table
        assert "SD-Karte steckt im PC" in side.device_label.text() and side.folder_list.count() == 2
        window.library_view.index.albums.append(LibraryAlbum(path=str(folder), title="Die Fuchsbande",
                                                             tracks=tracks, album_id="fuchs"))
        window._espuino_known.remember("777888999000", name="Fuchsbande", sd_folder="/Die Fuchsbande", play_mode=5)
        window._espuino_known.save()
        window._mediahub_reload()
        assert table.btn_transfer_missing.isEnabled()  # Uebertragen geht ohne ESPuino
        runs = []
        monkeypatch.setattr(window, "_espuino_run_transfer",
                            lambda plans, local, host, assignments, finished: runs.append((local, assignments)))
        window._espuino_transfer_missing()
        assert runs and runs[0][0] == str(sd) and all(a is None for a in runs[0][1])  # nicht zuweisen
    finally:
        window.close()
        window.deleteLater()



def test_pre_assign_without_espuino_then_assign_after_connecting(setup, monkeypatch):
    """Ohne ESPuino vormerken (Ordner schon auf der SD-Karte, kein Album in der Bibliothek);
    nach dem Verbinden weist "Fehlende uebertragen" die Karte zu"""
    from PyQt6.QtWidgets import QMessageBox
    from gui.espuino_device_view import needs_transfer
    from gui.main_window import MainWindow
    esp = setup["esp"]
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.Yes)
    window = MainWindow()
    try:
        window._espuino_connected = False
        window._mediahub_reload()
        reader = window._espuino_read_cards()
        reader.on_message('{"rfidId":"333444555666"}')  # z. B. vom ACR122U
        reader._set_path("/Hörspiele/Neu")
        assert reader.btn_assign.text() == "Vormerken" and reader.btn_assign.isEnabled()
        assert reader.try_assign()
        assert esp.posts == [] and "vorgemerkt" in reader.assign_note.text()
        assert window._espuino_known.get("333444555666").sd_folder == "/Hörspiele/Neu"
        reader.close()

        window._espuino_connected = True
        window._mediahub_reload()
        card = window._mediahub_card("333444555666")
        assert card.folder_present is True and not card.library_path and needs_transfer(card)
        window._espuino_transfer_missing()
        assert {"id": "333444555666", "fileOrUrl": "/Hörspiele/Neu", "playMode": 3} in esp.posts
        assert window._mediahub_card("333444555666").device_status == "assigned"
    finally:
        window.close()
        window.deleteLater()
