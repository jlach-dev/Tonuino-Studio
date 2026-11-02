import shutil
import subprocess

import pytest

pytest.importorskip("PyQt6")

from PyQt6.QtCore import QCoreApplication, QPoint, QPointF, QSettings, QSize, Qt, QUrl, QMimeData
from PyQt6.QtGui import QDragLeaveEvent, QDragMoveEvent, QDropEvent
from PyQt6.QtWidgets import QApplication, QWidget

from core import album_identity
from core.cover_archive import CoverArchive
from core.library import SETTINGS_SOURCES, Library
from core.metadata import MetadataManager
from gui.library_dnd import (
    MIME_ALBUM, DropArea, FolderListWidget, album_paths_from_mime, make_album_mime, make_tracks_mime,
)


@pytest.fixture(scope="module")
def app():
    QCoreApplication.setOrganizationName("Tonuino-Manager-Tests")
    QCoreApplication.setApplicationName("tests-library")
    application = QApplication.instance() or QApplication([])
    QSettings().clear()
    yield application
    QSettings().clear()


def make_album(root, name, count=2, album="", artist=""):
    import imageio_ffmpeg
    folder = root / name
    folder.mkdir(parents=True)
    subprocess.run([imageio_ffmpeg.get_ffmpeg_exe(), "-loglevel", "error", "-f", "lavfi", "-i",
                    "anullsrc=r=22050:cl=mono", "-t", "0.3", "-q:a", "9", str(folder / "01.mp3")], check=True)
    for i in range(2, count + 1):
        shutil.copy(folder / "01.mp3", folder / f"{i:02d}.mp3")
    for i in range(1, count + 1):
        MetadataManager().write_metadata(str(folder / f"{i:02d}.mp3"), album=album or name,
                                         artist=artist or "Interpret", title=f"Titel {i}")
    return folder


def test_mime_roundtrip_and_explorer_folders(tmp_path):
    album = tmp_path / "Album"
    album.mkdir()
    (album / "01.mp3").write_bytes(b"x")
    mime = make_album_mime([str(album)])
    assert mime.hasFormat(MIME_ALBUM) and album_paths_from_mime(mime) == [str(album)]

    explorer = QMimeData()
    explorer.setUrls([QUrl.fromLocalFile(str(album)), QUrl.fromLocalFile(str(album / "01.mp3")),
                      QUrl.fromLocalFile(str(tmp_path / "fehlt"))])
    assert album_paths_from_mime(explorer) == [str(album)]
    assert album_paths_from_mime(QMimeData()) == []


def test_folder_list_drop_targets(app, tmp_path):
    widget = FolderListWidget()
    widget.resize(200, 300)
    received = []
    widget.albums_dropped.connect(lambda paths, identity: received.append((paths, identity)))
    mime = make_album_mime([str(tmp_path)])

    def drop(pos):
        event = QDropEvent(QPointF(pos), Qt.DropAction.CopyAction, mime, Qt.MouseButton.LeftButton,
                           Qt.KeyboardModifier.NoModifier)
        widget.dropEvent(event)
        return event.isAccepted()

    assert not drop(widget.rect().center())  # ohne SD-Karte kein Drop
    widget.drop_enabled = True
    assert drop(widget.rect().center()) and received[-1] == ([str(tmp_path)], None)

    from PyQt6.QtWidgets import QListWidgetItem
    item = QListWidgetItem("01")
    item.setData(Qt.ItemDataRole.UserRole, 1)
    widget.addItem(item)
    assert drop(widget.visualItemRect(item).center()) and received[-1] == ([str(tmp_path)], 1)


def drag_move(widget, pos, mime):
    event = QDragMoveEvent(pos, Qt.DropAction.CopyAction, mime, Qt.MouseButton.LeftButton,
                           Qt.KeyboardModifier.NoModifier)
    widget.dragMoveEvent(event)
    return event.isAccepted()


def test_folder_list_highlights_drop_target(app, tmp_path):
    from PyQt6.QtWidgets import QListWidgetItem

    widget = FolderListWidget()
    widget.resize(200, 300)
    widget.show()
    highlight = widget.drop_highlight
    mime = make_album_mime([str(tmp_path)])
    for identity in (3, "mp3"):
        item = QListWidgetItem(str(identity))
        item.setData(Qt.ItemDataRole.UserRole, identity)
        widget.addItem(item)
    folder_rect = widget.visualItemRect(widget.item(0))
    special_rect = widget.visualItemRect(widget.item(1))

    # ohne SD-Karte: kein Drop, aber ein roter Hinweis auf der ganzen Liste
    assert not drag_move(widget, folder_rect.center(), mime)
    assert highlight.isVisible() and highlight.blocked and highlight.hint == "Zuerst SD-Karte öffnen"

    widget.drop_enabled = True
    assert drag_move(widget, folder_rect.center(), mime)
    assert highlight.isVisible() and highlight.geometry() == folder_rect
    assert highlight.hint == "In Ordner 03" and not highlight.blocked

    drag_move(widget, special_rect.center(), mime)
    assert highlight.geometry() == special_rect and highlight.blocked

    drag_move(widget, widget.viewport().rect().bottomLeft() - QPoint(-5, 5), mime)
    assert highlight.geometry() == widget.viewport().rect() and highlight.hint == "Neuer Ordner"

    tracks = make_tracks_mime([str(tmp_path / "01.mp3")])
    (tmp_path / "01.mp3").write_bytes(b"x")
    drag_move(widget, folder_rect.center(), tracks)
    assert highlight.hint == "An Ordner 03 anhängen"

    widget.dragLeaveEvent(QDragLeaveEvent())
    assert not highlight.isVisible()
    widget.close()


def test_drag_pixmap_scales_tile_and_counts_albums(app):
    from PyQt6.QtGui import QColor, QPixmap
    from gui.library_dnd import DRAG_IMAGE_WIDTH, drag_pixmap

    tile = QPixmap(176, 246)
    tile.fill(QColor("#ff0000"))
    single = drag_pixmap(tile)
    assert single.width() == DRAG_IMAGE_WIDTH and single.height() == round(246 * DRAG_IMAGE_WIDTH / 176)
    assert single.toImage().pixelColor(5, single.height() // 2).alpha() < 255  # leicht transparent

    several = drag_pixmap(tile, 3).toImage()
    badge = several.pixelColor(DRAG_IMAGE_WIDTH - 4 - 13, 4 + 13)
    assert badge.blue() > badge.red()  # blaues Abzeichen statt roter Kachel


def test_library_grid_drag_uses_tile_as_image(app, tmp_path, monkeypatch):
    from PyQt6.QtWidgets import QLabel, QListWidgetItem
    from gui import library_view

    started = {}

    class FakeDrag:
        def __init__(self, source):
            started["source"] = source

        def setMimeData(self, mime):
            started["mime"] = mime

        def setPixmap(self, pixmap):
            started["pixmap"] = pixmap

        def setHotSpot(self, point):
            started["hotspot"] = point

        def exec(self, action):
            started["action"] = action

    monkeypatch.setattr(library_view, "QDrag", FakeDrag)
    grid = library_view.LibraryGrid()
    for name in ("A", "B"):
        item = QListWidgetItem()
        item.setData(Qt.ItemDataRole.UserRole, str(tmp_path / name))
        item.setSizeHint(QSize(176, 246))
        grid.addItem(item)
        tile = QLabel(name)
        tile.resize(176, 246)
        grid.setItemWidget(item, tile)
    grid.item(0).setSelected(True)
    grid.item(1).setSelected(True)

    grid.startDrag(Qt.DropAction.CopyAction)

    assert album_paths_from_mime(started["mime"]) == [str(tmp_path / "A"), str(tmp_path / "B")]
    assert not started["pixmap"].isNull() and started["action"] == Qt.DropAction.CopyAction


def test_drop_area_highlight_follows_drag(app, tmp_path):
    area = DropArea()
    area.resize(400, 300)
    area.show()
    area.drop_enabled = True
    area.drop_hint = "In Ordner 07"
    mime = make_album_mime([str(tmp_path)])

    assert drag_move(area, area.rect().center(), mime)
    assert area.drop_highlight.isVisible() and area.drop_highlight.hint == "In Ordner 07"
    area.drop_highlight.grab()  # malt ohne Fehler

    event = QDropEvent(QPointF(area.rect().center()), Qt.DropAction.CopyAction, mime, Qt.MouseButton.LeftButton,
                       Qt.KeyboardModifier.NoModifier)
    area.dropEvent(event)
    assert not area.drop_highlight.isVisible()
    area.close()


def test_main_window_library_view_switch_badges_and_drop(app, tmp_path, monkeypatch):
    from gui import main_window as mw
    from gui.main_window import MainWindow

    source = tmp_path / "Musik"
    hexe = make_album(source, "Hexe", count=2, album="Die kleine Hexe", artist="Preußler")
    make_album(source, "Neu", count=1, album="Neues Album")
    data_dir = tmp_path / "data"
    settings = QSettings()
    settings.setValue("library/data_dir", str(data_dir))
    settings.setValue(SETTINGS_SOURCES, [str(source)])
    settings.setValue("library/write_id_tags", "no")
    # SD-Karte mit einem Ordner, der das Hexe-Album schon traegt
    sd_root = tmp_path / "sd"
    sd01 = make_album(sd_root, "01", count=1, album="Die kleine Hexe")
    (sd01 / "01.mp3").rename(sd01 / "001.mp3")
    album_identity.write_album_id([str(sd01 / "001.mp3")], "id-hexe")
    album_identity.write_album_id([str(p) for p in hexe.glob("*.mp3")], "id-hexe")
    CoverArchive(str(data_dir)).ensure_album("id-hexe", "Die kleine Hexe", "Preußler", path=str(hexe))
    Library(str(data_dir), [str(source)]).save(Library(str(data_dir), [str(source)]).scan())

    window = MainWindow()
    window.show()
    app.processEvents()
    assert window.stack.currentWidget() is window.welcome_widget
    assert window.library_view.grid.count() == 2  # Index aus dem Cache, noch ohne Einlesen

    # SD-Karte "oeffnen" wie _on_scan_finished nach dem Dialog
    from core.sd_card import SDCard
    window.sd_card = SDCard(str(sd_root))
    window.sd_card.scan()
    window._open_sd_dialog = type("D", (), {"close": lambda self: None})()
    window._on_scan_finished(True)
    assert window.folder_list.drop_enabled

    # Umschalter: Bibliothek einlesen, Kacheln mit Abzeichen
    window.view_switch.setCurrentIndex(1)
    assert window.stack.currentWidget() is window.library_view
    for _ in range(100):
        worker = window._library_worker
        if worker is not None and worker.isRunning():
            worker.wait(200)
        app.processEvents()
        if not window._library_rescan_pending and (worker is None or not worker.isRunning())                 and window._library_index.scanned and window.library_view.btn_rescan.isEnabled():
            break
    view = window.library_view
    assert view.grid.count() == 2
    by_name = {a.name: a for a in view.index.albums}
    assert [b[0] for b in view.badges_for(by_name["Die kleine Hexe"])] == ["auf SD als 01"]
    assert [b[0] for b in view.badges_for(by_name["Neues Album"])] == ["neu"]
    assert view.grid.mimeData([view.grid.item(0)]).hasFormat(MIME_ALBUM)

    view.search_edit.setText("neues")
    assert view.grid.count() == 1 and view.visible_albums()[0].name == "Neues Album"
    view.search_edit.clear()

    # zurueck zur SD-Ansicht: Ordner 01 zeigt die Verknuepfung samt Hinweis auf den fehlenden Track
    window.view_switch.setCurrentIndex(0)
    window._select_folder_in_list(1)
    app.processEvents()
    assert window.stack.currentWidget() is window.folder_widget
    assert window.folder_library_hint.isVisibleTo(window)
    assert window.folder_library_hint.text().startswith("aus Bibliothek: Die kleine Hexe")
    assert "1 Track(s) mehr" in window.folder_library_hint.text()

    # Drop auf freie Flaeche: neuer Ordner 02 mit dem neuen Album (ohne Rueckfrage, Entscheidung gespeichert)
    monkeypatch.setattr(mw.QMessageBox, "warning", lambda *a, **k: None)
    window._on_albums_dropped([str(source / "Neu")], None)
    for _ in range(50):
        job = window._import_job
        if job is None:
            break
        job["worker"].wait(500)
        app.processEvents()
    assert window._import_job is None
    assert sorted(p.name for p in (sd_root / "02").glob("*.mp3")) == ["001.mp3"]
    assert window.current_folder.index == 2 and window.folder_title.text() == "Neues Album"
    new_id = album_identity.read_album_id([str(sd_root / "02" / "001.mp3")])
    assert new_id and window._sd_album_map.get(new_id) == 2
    assert album_identity.read_album_id([str(source / "Neu" / "01.mp3")]) is None  # Bibliothek unangetastet

    # Drop auf Ordner 01 mit "Anhaengen"
    monkeypatch.setattr(window, "_ask_replace_or_append", lambda *_: "append")
    window._on_albums_dropped([str(hexe)], 1)
    for _ in range(50):
        job = window._import_job
        if job is None:
            break
        job["worker"].wait(500)
        app.processEvents()
    assert sorted(p.name for p in (sd_root / "01").glob("*.mp3")) == ["001.mp3", "002.mp3", "003.mp3"]
    assert window.current_folder.index == 1
    window.close()
    app.processEvents()


def test_library_tile_context_menu(app, tmp_path):
    from core.library import LibraryAlbum, LibraryIndex
    from gui.library_view import LibraryView

    view = LibraryView()
    view.resize(900, 600)
    hexe = LibraryAlbum(path=str(tmp_path / "Hexe"), title="Die kleine Hexe", tracks=["a"])
    raeuber = LibraryAlbum(path=str(tmp_path / "Räuber"), title="Räuber", tracks=["b"])
    nas = LibraryAlbum(path=r"\nas\musik\Weg", title="Nicht erreichbar", tracks=["c"], offline=True)
    view.index = LibraryIndex(albums=[hexe, raeuber, nas])
    view._refresh()
    view.show()
    app.processEvents()
    received = {"cover": [], "transfer": [], "recognize": []}
    view.cover_requested.connect(received["cover"].append)
    view.transfer_requested.connect(received["transfer"].append)
    view.recognize_requested.connect(lambda album, paths: received["recognize"].append((album, paths)))

    def menu_at(row):
        menu = view.tile_menu(view.grid.visualItemRect(view.grid.item(row)).center())
        return menu, {a.text(): a for a in menu.actions() if a.text()}

    menu, actions = menu_at(0)
    assert list(actions) == ["Öffnen", "Cover …", "Tags nachtragen …", "Stücke erkennen …", "Einsortieren …",
                             "Auf SD-Karte übertragen", "Im Explorer zeigen", "Pfad kopieren"]
    assert not actions["Auf SD-Karte übertragen"].isEnabled()  # keine SD-Karte offen
    actions["Cover …"].trigger()
    actions["Stücke erkennen …"].trigger()
    album = view.index.albums[0]
    assert received["cover"] == [album] and received["recognize"] == [(album, [])]

    # Mehrfachauswahl: Uebertragen gilt fuer alle markierten (erreichbaren) Alben
    view.set_sd_available(True)
    view.grid.selectAll()
    menu, actions = menu_at(0)
    transfer = actions["Auf SD-Karte übertragen (2 Alben)"]
    assert transfer.isEnabled()
    transfer.trigger()
    assert sorted(received["transfer"][0]) == sorted([hexe.path, raeuber.path])

    # Offline-Album: nur "Pfad kopieren"
    offline_row = next(i for i in range(view.grid.count())
                       if view.grid.item(i).data(Qt.ItemDataRole.UserRole) == nas.path)
    view.grid.clearSelection()
    menu, actions = menu_at(offline_row)
    assert [name for name, a in actions.items() if a.isEnabled()] == ["Pfad kopieren"]
    actions["Pfad kopieren"].trigger()
    assert QApplication.clipboard().text() == nas.path
    view.close()


def test_library_tile_selection_is_visible(app, tmp_path):
    """Die Kachel verdeckt die Markierung des Listeneintrags - sie zeigt den Zustand selbst"""
    from core.library import LibraryAlbum, LibraryIndex
    from gui.library_view import LibraryView
    from gui.styles import MAIN_STYLESHEET

    view = LibraryView()
    view.setStyleSheet(MAIN_STYLESHEET)  # Rahmen und Padding kommen aus dem Stylesheet
    view.resize(900, 600)
    view.index = LibraryIndex(albums=[LibraryAlbum(path=str(tmp_path / name), title=name, tracks=["a"])
                                      for name in ("Hexe", "Räuber", "Sterne")])
    view._refresh()
    view.show()
    app.processEvents()
    tiles = [view.grid.itemWidget(view.grid.item(row)) for row in range(view.grid.count())]
    assert view.count_label.text() == "3 Alben"
    assert not any(tile.is_selected() for tile in tiles)
    cover_pos = tiles[0].findChild(QWidget, "libraryTileCover").pos()

    view.grid.item(0).setSelected(True)
    view.grid.item(2).setSelected(True)
    app.processEvents()
    assert [tile.is_selected() for tile in tiles] == [True, False, True]
    assert [tile.check_mark.isVisibleTo(tile) for tile in tiles] == [True, False, True]
    assert view.count_label.text() == "3 Alben · 2 markiert"
    # Breiterer Rahmen, aber nichts verrutscht
    assert tiles[0].findChild(QWidget, "libraryTileCover").pos() == cover_pos

    view.grid.clearSelection()
    app.processEvents()
    assert not any(tile.is_selected() for tile in tiles)
    assert view.count_label.text() == "3 Alben"
    view.close()


@pytest.fixture
def window_with_folder(app, tmp_path, monkeypatch):
    """Hauptfenster mit Ordner 01 (zwei Tracks, eigener Name) und einem anderen Album in der Bibliothek"""
    from core.sd_card import SDCard
    from gui import main_window as mw
    from gui.main_window import MainWindow

    QSettings().clear()
    source = tmp_path / "Musik"
    other = make_album(source, "Anderes", count=3, album="Anderes Album")
    data_dir = tmp_path / "data"
    settings = QSettings()
    settings.setValue("library/data_dir", str(data_dir))
    settings.setValue(SETTINGS_SOURCES, [str(source)])
    settings.setValue("library/write_id_tags", "no")
    Library(str(data_dir), [str(source)]).save(Library(str(data_dir), [str(source)]).scan())

    sd_root = tmp_path / "sd"
    folder = make_album(sd_root, "01", count=2, album="Altes Album")
    (folder / "01.mp3").rename(folder / "001.mp3")
    (folder / "02.mp3").rename(folder / "002.mp3")
    for name in ("001.mp3", "002.mp3"):
        MetadataManager().write_metadata(str(folder / name), folder_name="Mein Name")

    monkeypatch.setattr(mw.QMessageBox, "warning", lambda *a, **k: None)
    window = MainWindow()
    window.sd_card = SDCard(str(sd_root))
    window.sd_card.scan()
    window._open_sd_dialog = type("D", (), {"close": lambda self: None})()
    window._on_scan_finished(True)
    window._select_folder_in_list(1)
    app.processEvents()
    yield window, folder, other
    window.close()
    QSettings().clear()


def finish_import(app, window):
    for _ in range(100):
        job = window._import_job
        if job is None:
            break
        job["worker"].wait(500)
        app.processEvents()
    assert window._import_job is None
    app.processEvents()


def titles(folder):
    return [MetadataManager().read_metadata(str(p)).title for p in sorted(folder.glob("*.mp3"))]


def test_replace_swaps_tracks_and_drops_the_manual_name(app, window_with_folder, monkeypatch):
    """Ersetzen ueber das Hauptfenster: danach liegt ein anderes Album im Ordner, also gilt dessen Name"""
    from core.library_import import MODE_REPLACE

    window, folder, other = window_with_folder
    assert window.folder_title.text() == "Mein Name"
    monkeypatch.setattr(window, "_ask_replace_or_append", lambda *_: MODE_REPLACE)
    window._on_albums_dropped([str(other)], 1)
    finish_import(app, window)

    assert sorted(p.name for p in folder.glob("*.mp3")) == ["001.mp3", "002.mp3", "003.mp3"]
    assert titles(folder) == ["Titel 1", "Titel 2", "Titel 3"]
    assert not list(folder.glob(".tmp_*"))
    assert MetadataManager().read_metadata(str(folder / "001.mp3")).album == "Anderes Album"
    assert window.current_folder.track_count == 3
    assert window.folder_title.text() == "Anderes Album"


def test_cancelled_replace_keeps_old_tracks(app, window_with_folder, monkeypatch):
    from core.library_import import MODE_REPLACE
    from gui import main_window_library as module

    class CancelledAtOnce(module.TrackAddWorker):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.cancel()

    window, folder, other = window_with_folder
    before = {p.name: p.read_bytes() for p in folder.glob("*.mp3")}
    monkeypatch.setattr(module, "TrackAddWorker", CancelledAtOnce)
    monkeypatch.setattr(window, "_ask_replace_or_append", lambda *_: MODE_REPLACE)
    window._on_albums_dropped([str(other)], 1)
    finish_import(app, window)

    assert {p.name: p.read_bytes() for p in folder.glob("*.mp3")} == before
    assert not list(folder.glob(".tmp_*"))
    assert "bisherigen Tracks" in window.status_bar.currentMessage()


def test_append_keeps_the_manual_name(app, window_with_folder, monkeypatch):
    from core.library_import import MODE_APPEND

    window, folder, other = window_with_folder
    monkeypatch.setattr(window, "_ask_replace_or_append", lambda *_: MODE_APPEND)
    window._on_albums_dropped([str(other)], 1)
    finish_import(app, window)

    assert len(list(folder.glob("*.mp3"))) == 5
    assert window.folder_title.text() == "Mein Name"
    assert MetadataManager().read_metadata(str(folder / "005.mp3")).folder_name == "Mein Name"


def test_tags_written_keep_the_manual_name(app, window_with_folder):
    window, _folder, _other = window_with_folder
    window._after_tags_written(window.current_folder, "Neuer Album-Tag", 2)
    assert window.folder_title.text() == "Mein Name"
