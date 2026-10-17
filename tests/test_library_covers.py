"""Album-Cover in der Bibliothek: alle Formate, Stapellauf aus dem Archiv, Cover-Manager im Bibliotheksmodus"""

import os

import pytest

from core import album_identity
from core.cover_archive import KIND_FILE, ROLE_ALBUM_COVER, ROLE_CARD_COVER, CoverArchive
from core.library import LibraryAlbum, LibraryIndex, read_track_tags, write_album_cover, write_cover_any
from core.library_import import embed_archive_covers
from core.metadata import MetadataManager
from tests.test_library import make_album, png_bytes
from tests.test_library_tags import make_flac


def test_write_cover_any_mp3_and_flac(tmp_path):
    folder = make_album(tmp_path, "Album", count=1)
    mp3 = str(folder / "01.mp3")
    flac = str(make_flac(folder / "02.flac"))
    assert write_cover_any(mp3, png_bytes(), "image/png")
    assert write_cover_any(flac, png_bytes(), "image/png")
    assert read_track_tags(mp3).cover == png_bytes()
    assert read_track_tags(flac).cover == png_bytes()
    # MetadataManager reicht andere Formate an core.library durch
    manager = MetadataManager()
    assert manager.get_cover_bytes(flac) == png_bytes()
    assert manager.set_cover_bytes(flac, png_bytes(color=(1, 2, 3)), "image/png")
    assert manager.read_metadata_and_cover(flac)[0].has_cover
    # Album-ID auch in FLAC
    assert album_identity.write_album_id([flac], "abc123") == 1
    assert album_identity.read_album_id([flac]) == "abc123"


def test_write_album_cover_writes_cover_jpg(tmp_path):
    folder = make_album(tmp_path, "Album", count=2)
    paths = [str(folder / "01.mp3"), str(folder / "02.mp3")]
    assert write_album_cover(paths, str(folder), b"\xff\xd8\xffjpeg") == 2
    assert (folder / "cover.jpg").read_bytes() == b"\xff\xd8\xffjpeg"


def test_embed_archive_covers(tmp_path):
    archive = CoverArchive(str(tmp_path / "data"))
    with_cover = make_album(tmp_path / "Musik", "Hexe", count=2, album="Die kleine Hexe")
    without = make_album(tmp_path / "Musik", "Räuber", count=1, album="Räuber Hotzenplotz")
    archive.ensure_album("id1", "Die kleine Hexe", path=str(with_cover))
    entry = archive.add_cover("id1", png_bytes(size=(400, 300)), KIND_FILE, source="test")
    archive.set_role("id1", entry.id, ROLE_ALBUM_COVER)
    archive.ensure_album("id2", "Räuber Hotzenplotz", path=str(without))
    index = LibraryIndex(albums=[
        LibraryAlbum(path=str(with_cover), tracks=[str(with_cover / "01.mp3"), str(with_cover / "02.mp3")],
                     album_id="id1"),
        LibraryAlbum(path=str(without), tracks=[str(without / "01.mp3")], album_id="id2"),
        LibraryAlbum(path=str(without), tracks=[str(without / "01.mp3")], album_id="id1", has_cover=True),
    ])
    seen = []
    result = embed_archive_covers(index, archive, progress=lambda i, n, name: seen.append((i, n, name)))
    assert (result.albums, result.tracks, result.skipped) == (1, 2, 1)
    assert seen == [(1, 2, "Hexe"), (2, 2, "Räuber")]
    assert (with_cover / "cover.jpg").exists() and not (without / "cover.jpg").exists()
    cover = MetadataManager().get_cover_bytes(str(with_cover / "02.mp3"))
    assert cover and cover[:3] == b"\xff\xd8\xff"  # quadratisches JPEG
    assert index.albums[0].has_cover


def test_cover_manager_library_mode(tmp_path, monkeypatch):
    pytest.importorskip("PyQt6")
    from PyQt6.QtCore import QCoreApplication, QSettings
    from PyQt6.QtWidgets import QApplication
    from core import card_store
    from core.sd_card import Folder, Track
    from gui import cover_manager_dialog as cm

    QCoreApplication.setOrganizationName("Tonuino-Manager-Tests")
    app = QApplication.instance() or QApplication([])
    monkeypatch.setattr(cm, "gemini_client", lambda: None)
    archive = CoverArchive(str(tmp_path / "data"))
    folder_path = make_album(tmp_path / "Musik", "Hexe", count=1, album="Die kleine Hexe", artist="Preußler")
    flac = make_flac(folder_path / "02.flac")
    paths = [str(folder_path / "01.mp3"), str(flac)]
    archive.ensure_album("lib1", "Die kleine Hexe", "Preußler", path=str(folder_path))
    entry = archive.add_cover("lib1", png_bytes(size=(400, 400)), KIND_FILE, source="test")

    tracks = [Track(index=i, filename=os.path.basename(p), filepath=p) for i, p in enumerate(paths, start=1)]
    folder = Folder(index=None, path=str(folder_path), tracks=tracks, name="Hexe")
    dialog = cm.CoverManagerDialog(folder, "Die kleine Hexe", MetadataManager(), archive, focus="album",
                                   library=True, album_id="lib1")
    assert dialog.album_id == "lib1"
    assert dialog._card_dir == str(archive.card_dir("lib1"))
    app.processEvents()
    archived = next(c for c in dialog.candidates if c.entry is not None and c.entry.id == entry.id)
    dialog.selected = archived

    dialog._set_album_cover()
    assert (folder_path / "cover.jpg").exists()
    for path in paths:
        assert MetadataManager().get_cover_bytes(path)
    assert album_identity.read_album_id([str(flac)]) == "lib1"
    assert ROLE_ALBUM_COVER in archive.covers("lib1")[0].roles

    dialog.title_edit.setPlainText("Die kleine Hexe")
    dialog._set_card_cover()
    meta = card_store.load_meta(str(archive.card_dir("lib1")))
    assert meta is not None and meta.title == "Die kleine Hexe" and meta.album_id == "lib1"
    assert not card_store.has_card(str(folder_path))  # nichts im Bibliotheksordner
    assert ROLE_CARD_COVER in archive.covers("lib1")[0].roles
    for worker in (dialog._research_worker, dialog._search_worker):
        if worker is not None:
            worker.wait(5000)
    app.processEvents()
    dialog.done(0)

    # zweites Oeffnen: Gestaltung kommt aus dem Archiv
    dialog2 = cm.CoverManagerDialog(folder, "Die kleine Hexe", MetadataManager(), archive, library=True)
    assert dialog2.album_id == "lib1" and dialog2.title_edit.toPlainText() == "Die kleine Hexe"
    assert any(c.section == cm.SECTION_CURRENT and c.is_sd_card_art for c in dialog2.candidates)
    app.processEvents()
    for worker in (dialog2._research_worker, dialog2._search_worker):
        if worker is not None:
            worker.wait(5000)
    app.processEvents()
    dialog2.done(0)
    QSettings().clear()
