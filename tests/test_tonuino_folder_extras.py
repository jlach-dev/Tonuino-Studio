"""TonUINO-Modus: aus dem ESPuino-Modus uebernommen - Serie und Filter in der Ordnerliste,
Serie im Kopf der Ordnerseite, Warnung beim Ziehen eines schon vorhandenen Albums,
Groesse in der Loesch-Rueckfrage"""
from pathlib import Path

import pytest

pytest.importorskip("PyQt6")
from PyQt6.QtCore import QCoreApplication, QSettings
from PyQt6.QtWidgets import QApplication

from core.library import LibraryAlbum, LibraryIndex
from core.sd_card import SDCard


@pytest.fixture(scope="module")
def app():
    QCoreApplication.setOrganizationName("Tonuino-Manager-Tests")
    QCoreApplication.setApplicationName("tests-tonuino-folder-extras")
    application = QApplication.instance() or QApplication([])
    QSettings().clear()
    yield application
    QSettings().clear()


def make_card(root: Path, folders=(1, 2, 3)) -> SDCard:
    for index in folders:
        path = root / f"{index:02d}"
        path.mkdir(parents=True)
        (path / "001.mp3").write_bytes(b"x" * 2048)
    card = SDCard(str(root))
    card.scan()
    return card


def open_window(card, tmp_path):
    from gui.main_window import MainWindow
    window = MainWindow()
    albums = [LibraryAlbum(path=str(tmp_path / "lib" / "Fuchs"), title="Fuchs", artist="Die Fuchsbande",
                           tracks=[], album_id="a"),
              LibraryAlbum(path=str(tmp_path / "lib" / "Conni"), title="Conni", artist="Conni", tracks=[],
                           album_id="b")]
    window.library_view.index = LibraryIndex(albums=albums)
    window._sd_album_map = {"a": 1, "b": 2}
    window.sd_card = card
    window._populate_folder_list()
    return window


def test_folder_list_shows_series_and_filters(app, tmp_path):
    window = open_window(make_card(tmp_path / "sd"), tmp_path)
    try:
        items = [window.folder_list.item(r) for r in range(window.folder_list.count())]
        labels = [window.folder_list.itemWidget(i).series_label.text() for i in items]
        assert labels == ["Die Fuchsbande", "Conni", ""]
        combo = window.folder_series_combo
        assert not combo.isHidden()
        assert [combo.itemText(i) for i in range(combo.count())] == ["Alle Serien", "Conni", "Die Fuchsbande",
                                                                       "Ohne Serie"]
        combo.setCurrentIndex(combo.findData("Conni"))
        assert [i.isHidden() for i in items] == [True, False, True]
        combo.setCurrentIndex(combo.findData(""))  # ohne Serie
        assert [i.isHidden() for i in items] == [True, True, False]
        combo.setCurrentIndex(0)
        window.folder_filter_edit.setText("fuchs")
        assert [i.isHidden() for i in items] == [False, True, True]
        window.folder_filter_edit.setText("03")
        assert [i.isHidden() for i in items] == [True, True, False]
        window.folder_filter_edit.setText("")
        window._select_folder_in_list(1)
        assert "Die Fuchsbande" in window.folder_subtitle.text()
    finally:
        window.close()
        window.deleteLater()


def test_drop_album_already_on_card_asks_and_skips(app, tmp_path, monkeypatch):
    window = open_window(make_card(tmp_path / "sd"), tmp_path)
    try:
        queued, asked = [], []
        monkeypatch.setattr(window, "_start_next_import", lambda: queued.extend(window._import_queue))
        monkeypatch.setattr(window, "_ask_already_on_card",
                            lambda present, total: asked.append((present, total)) or "skip")
        new = str(tmp_path / "lib" / "Neu")
        window._on_albums_dropped([str(tmp_path / "lib" / "Fuchs"), new], None)
        assert asked[0][0] == {str(tmp_path / "lib" / "Fuchs"): (1, "Fuchs")} and asked[0][1] == 2
        assert [q[0] for q in queued] == [new]
        window._import_queue.clear()
        queued.clear()
        monkeypatch.setattr(window, "_ask_already_on_card", lambda present, total: None)  # Abbrechen
        window._on_albums_dropped([str(tmp_path / "lib" / "Fuchs")], None)
        assert queued == []
    finally:
        window.close()
        window.deleteLater()


def test_delete_question_names_size(app, tmp_path):
    card = make_card(tmp_path / "sd")
    window = open_window(card, tmp_path)
    try:
        text = window._delete_confirmation_text([card.folders[1]])
        assert "1 Track(s) (2 KB)" in text
        text = window._delete_confirmation_text([card.folders[1], card.folders[2]])
        assert "2 Track(s) (4 KB)" in text
    finally:
        window.close()
        window.deleteLater()
