"""ESPuino-Modus: Hauptfenster, Cover-Manager, Druckbogen und KI-Stapel fuer
Karten aus dem ESPuino-Mediahub (erfundene Daten, stille MP3s)"""

import json

import pytest

pytest.importorskip("PyQt6")

from PIL import Image
from PyQt6.QtCore import QCoreApplication, QSettings
from PyQt6.QtWidgets import QApplication

from core import album_identity, card_store
from core.app_mode import ESPUINO_APP_NAME, MODE_ESPUINO, MODE_TONUINO, SETTINGS_APP_MODE
from core.branding import APP_NAME
from core.cover_archive import CoverArchive
from core.mediahub_cards import SETTINGS_DB_PATH, SETTINGS_MEDIA_ROOT, build_folder, load_cards
from core.metadata import MetadataManager
from tests.test_library import make_album

CARD_A = "001002003004"
CARD_B = "100200300400"


@pytest.fixture(scope="module")
def app():
    QCoreApplication.setOrganizationName("Tonuino-Manager-Tests")
    QCoreApplication.setApplicationName("tests-mediahub")
    application = QApplication.instance() or QApplication([])
    QSettings().clear()
    yield application
    QSettings().clear()


@pytest.fixture
def hub(tmp_path, app):
    """Mediathek mit zwei Alben, db.json mit zwei Karten (eine als Wiedergabeliste)"""
    media = tmp_path / "media"
    make_album(media / "Hörspiele", "Fuchsbande", count=2, album="Die Fuchsbande", artist="Erfunden")
    make_album(media / "Musik", "Lieder", count=2, album="Lieder", artist="Erfunden")
    db = tmp_path / "hubdata" / "db.json"
    db.parent.mkdir()
    db.write_text(json.dumps({"settings": {}, "devices": {"esp1": {"alias": "Kinderzimmer"}}, "cards": {
        f"esp1/{CARD_A}": {"esp_id": "esp1", "card_id": CARD_A, "status": "assigned", "name": "Fuchsbande",
                           "kind": "files", "files": [{"path": "Hörspiele/Fuchsbande/01.mp3"},
                                                      {"path": "Hörspiele/Fuchsbande/02.mp3"}],
                           "updated_at": "2026-10-01T10:00:00+00:00"},
        f"esp1/{CARD_B}": {"esp_id": "esp1", "card_id": CARD_B, "status": "assigned", "name": "Mix",
                           "kind": "files", "files": [{"path": "Hörspiele/Fuchsbande/01.mp3"},
                                                      {"path": "Musik/Lieder/02.mp3"},
                                                      {"path": "Musik/fehlt.mp3"}],
                           "updated_at": "2026-10-02T10:00:00+00:00"},
    }}), encoding="utf-8")
    settings = QSettings()
    settings.clear()
    settings.setValue(SETTINGS_DB_PATH, str(db))
    settings.setValue(SETTINGS_MEDIA_ROOT, str(media))
    settings.setValue("library/data_dir", str(tmp_path / "studio-data"))
    yield {"media": media, "db": db, "archive": CoverArchive(str(tmp_path / "studio-data"))}
    settings.clear()


def test_tonuino_mode_is_default_and_unchanged(app, hub):
    from gui.main_window import MainWindow
    window = MainWindow()
    try:
        assert window.windowTitle() == APP_NAME
        assert window.view_switch.tabText(0) == "SD-Karte"
        assert not window.folder_list.isHidden() and not window.rfid_panel.isHidden()
        assert [a.data() for a in window.mode_menu.actions() if a.isChecked()] == [MODE_TONUINO]
        assert not hasattr(window, "mediahub_page")
    finally:
        window.close()
        window.deleteLater()


def test_espuino_mode_shows_mediahub_cards(app, hub):
    QSettings().setValue(SETTINGS_APP_MODE, MODE_ESPUINO)
    from gui.main_window import MainWindow
    window = MainWindow()
    try:
        assert window.windowTitle() == ESPUINO_APP_NAME
        assert window.sidebar_title.text() == ESPUINO_APP_NAME
        assert window.view_switch.tabText(0) == "Mediahub-Karten"
        for widget in (window.btn_open_sd, window.folder_list, window.rfid_panel, window.btn_purge_card):
            assert widget.isHidden()
        for action in (window.action_system_files, window.action_wipe_card, window.action_tag_lookup,
                       window.action_series_cover_batch, window.reader_menu.menuAction()):
            assert not action.isVisible()
        assert window.stack.currentWidget() is window.mediahub_page
        cards = window.mediahub_sidebar.card_list
        assert cards.count() == 2
        assert "2 Karten" in window.mediahub_page.overview_text.text()

        window.mediahub_sidebar.select(CARD_B)
        page = window.mediahub_page
        assert page.card.card_id == CARD_B
        assert "1 nicht in der Mediathek" in page.meta_label.text()
        assert page.file_list.item(2).text().endswith("(fehlt)")

        window.view_switch.setCurrentIndex(1)
        assert window.stack.currentWidget() is window.library_view
        window.view_switch.setCurrentIndex(0)
        assert window.stack.currentWidget() is page
    finally:
        window.close()
        window.deleteLater()


def test_cover_manager_for_card_never_tags_media_files(app, hub):
    from gui.cover_manager_dialog import CoverManagerDialog
    archive = hub["archive"]
    card = next(c for c in load_cards(str(hub["db"])).cards if c.card_id == CARD_B)
    folder = build_folder(card, str(hub["media"]), archive)
    dialog = CoverManagerDialog(folder, card.label, MetadataManager(), archive, None,
                                album_id=folder.album_id, mediahub=True)
    try:
        assert dialog.album_id == "mediahub" + CARD_B
        assert "Mediahub-Karte: Mix" in dialog.windowTitle()
        assert dialog._card_dir == folder.card_dir
        assert not dialog.btn_album_cover.isEnabled() and dialog.btn_tags.isHidden()
        dialog._persist_album()
        assert album_identity.read_album_id([t.filepath for t in folder.tracks]) is None
        record = archive.load_album(folder.album_id)
        assert record is not None and record.paths == []
        assert archive.find_album("Mix") is None  # Karten sind keine Alben
    finally:
        dialog.deleteLater()


def test_batch_worker_and_print_sheet_for_cards(app, hub, monkeypatch):
    from core import cover_generator, cover_research
    from core.cover_research import CoverBrief
    from core.cover_generator import SOURCE_AI
    from gui.print_sheet_dialog import PrintSheetDialog
    from gui.workers import CoverBatchWorker

    archive = hub["archive"]
    monkeypatch.setattr(cover_research, "research", lambda info, *a, **k: CoverBrief(summary="x"))
    monkeypatch.setattr(cover_generator, "generate_art",
                        lambda source, req, client=None, model="": Image.new("RGB", (400, 600), (90, 120, 40)))
    data = load_cards(str(hub["db"]))
    folders = [build_folder(c, str(hub["media"]), archive) for c in data.cards]
    worker = CoverBatchWorker(folders, MetadataManager(), object(), "t", "img", SOURCE_AI, "Aquarell", "portrait",
                              archive=archive)
    done, finished = [], []
    worker.folder_done.connect(done.append)
    worker.completed.connect(finished.append)
    worker.run()

    assert finished == [2] and done == folders
    for folder in folders:
        meta = card_store.load_meta(folder.card_dir)
        assert meta is not None and meta.album_id == folder.album_id
        assert meta.title == folder.name
        assert album_identity.read_album_id([t.filepath for t in folder.tracks]) is None
    assert not list(hub["media"].rglob("card.json"))  # nichts in die Mediathek geschrieben

    folders = [build_folder(c, str(hub["media"]), archive) for c in data.cards]
    dialog = PrintSheetDialog(folders, {}, None)
    try:
        labels = [dialog.folder_list.item(i).text() for i in range(dialog.folder_list.count())]
        assert labels == ["Fuchsbande", "Mix"]
        assert len(dialog._items()) == 2
    finally:
        dialog.deleteLater()
