"""Bibliothek: Mehrfachauswahl der Quellen, Album oeffnen, Tracks ziehen, Teilimport"""

import pytest

from core.library import Library, read_album_tracks
from core.library_import import group_tracks_by_album, import_album
from core.metadata import MetadataManager
from core.sd_card import SDCard
from tests.test_library import make_album


def test_source_conflicts(tmp_path):
    pytest.importorskip("PyQt6")
    from gui.library_settings_dialog import source_conflict

    base = tmp_path / "Musik"
    (base / "Kinder").mkdir(parents=True)
    existing = [str(base)]
    assert source_conflict(str(base), existing) == "ist schon eingetragen"
    assert "innerhalb" in source_conflict(str(base / "Kinder"), existing)
    assert "enthält" in source_conflict(str(tmp_path), existing)
    assert source_conflict(str(tmp_path / "Anderes"), existing) is None


def test_read_album_tracks_sorted_by_track_number(tmp_path):
    folder = make_album(tmp_path, "Hexe", count=3, album="Die kleine Hexe", artist="Preußler")
    # Datei 01 bekommt Tracknummer 3: Sortierung folgt der Nummer, nicht dem Dateinamen
    MetadataManager().write_metadata(str(folder / "01.mp3"), track_number=3, title="Letzter")
    MetadataManager().write_metadata(str(folder / "03.mp3"), track_number=1, title="Erster")
    tracks = read_album_tracks(str(folder))
    assert [t.title for t in tracks] == ["Erster", "Track 2", "Letzter"]
    assert [t.number for t in tracks] == [1, 2, 3]
    assert tracks[0].display == "01  Erster" and tracks[0].duration > 0


def test_tracks_mime_and_explorer_files(tmp_path):
    pytest.importorskip("PyQt6")
    from PyQt6.QtCore import QMimeData, QUrl
    from gui.library_dnd import MIME_TRACKS, drop_payload_from_mime, make_album_mime, make_tracks_mime

    folder = make_album(tmp_path, "Hexe", count=2)
    files = [str(folder / "01.mp3"), str(folder / "02.mp3")]
    mime = make_tracks_mime(files)
    assert mime.hasFormat(MIME_TRACKS)
    payload = drop_payload_from_mime(mime)
    assert payload.tracks == files and payload.albums == [] and payload

    album_payload = drop_payload_from_mime(make_album_mime([str(folder)]))
    assert album_payload.albums == [str(folder)] and album_payload.tracks == []

    explorer = QMimeData()
    explorer.setUrls([QUrl.fromLocalFile(files[1]), QUrl.fromLocalFile(str(folder))])
    mixed = drop_payload_from_mime(explorer)
    assert mixed.tracks == [files[1]] and mixed.albums == [str(folder)]
    assert not drop_payload_from_mime(QMimeData())


def test_group_tracks_by_album(tmp_path):
    a = make_album(tmp_path, "A", count=2)
    b = make_album(tmp_path, "B", count=1)
    groups = group_tracks_by_album([str(a / "02.mp3"), str(b / "01.mp3"), str(a / "01.mp3")])
    assert groups == [(str(a), [str(a / "02.mp3"), str(a / "01.mp3")]), (str(b), [str(b / "01.mp3")])]


def test_import_only_selected_tracks_links_album(tmp_path):
    from core.audio_converter import AudioConverter
    from core.cover_archive import CoverArchive

    source = tmp_path / "Musik"
    folder = make_album(source, "Hexe", count=3, album="Die kleine Hexe", artist="Preußler")
    (tmp_path / "sd").mkdir()
    sd = SDCard(str(tmp_path / "sd"))
    sd.scan()
    target = sd.create_folder(1)
    archive = CoverArchive(str(tmp_path / "data"))
    album = Library(str(tmp_path / "data"), [str(source)]).scan().albums[0]

    result = import_album(sd, target, album, AudioConverter(), MetadataManager(), archive, False,
                          tracks=[str(folder / "02.mp3"), str(folder / "03.mp3")])
    assert result.added == 2 and target.track_count == 2
    assert result.album_id and archive.load_album(result.album_id).title == "Die kleine Hexe"
    assert MetadataManager().read_metadata(target.tracks[0].filepath).title == "Track 2"


def test_library_view_album_panel(tmp_path):
    pytest.importorskip("PyQt6")
    from PyQt6.QtCore import QCoreApplication, QSettings
    from PyQt6.QtWidgets import QApplication
    from gui.library_dnd import MIME_TRACKS
    from gui.library_view import LibraryView

    QCoreApplication.setOrganizationName("Tonuino-Manager-Tests")
    app = QApplication.instance() or QApplication([])
    source = tmp_path / "Musik"
    make_album(source, "Hexe", count=2, album="Die kleine Hexe", artist="Preußler")
    lib = Library(str(tmp_path / "data"), [str(source)])
    index = lib.scan()
    view = LibraryView()
    view.set_library(lib, index)

    view.open_album(index.albums[0].path)
    assert view.open_album_path == index.albums[0].path
    for _ in range(50):
        app.processEvents()
        if view._tracks_worker is not None:
            view._tracks_worker.wait(100)
    panel = view.album_panel
    assert panel.track_list.count() == 2 and panel.title_label.text() == "Die kleine Hexe"
    panel.track_list.selectAll()
    mime = panel.track_list.mimeData(panel.track_list.selectedItems())
    assert mime.hasFormat(MIME_TRACKS) and len(panel.track_list.selected_paths()) == 2

    view.show_overview()
    assert view.open_album_path is None
    view.shutdown()
    QSettings().clear()
