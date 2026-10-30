"""Bibliothek: Tags nachtragen fuer Alben aller Formate (C1)"""

import shutil
import subprocess

import pytest

from core import album_identity, tag_lookup
from core.library import Library, read_track_tags, write_tags_any
from core.metadata import MetadataManager
from tests.test_library import _ffmpeg, make_album


def make_flac(path, seconds=0.3):
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run([_ffmpeg(), "-loglevel", "error", "-f", "lavfi", "-i", "anullsrc=r=22050:cl=mono", "-t",
                    str(seconds), str(path)], check=True)
    return path


def test_write_tags_any_flac_and_mp3(tmp_path):
    flac = make_flac(tmp_path / "a.flac")
    assert write_tags_any(str(flac), {"title": "Heia", "album": "Die kleine Hexe", "artist": "Preußler",
                                      "track_number": 2, "total_tracks": 5, "year": "2007"},
                          {album_identity.MB_ALBUM_ID_DESC: "rel-1", "TONUINO_ALBUM_ID": "id-9"})
    tags = read_track_tags(str(flac))
    assert (tags.title, tags.album, tags.artist, tags.track_number) == ("Heia", "Die kleine Hexe", "Preußler", 2)
    assert tags.album_id == "id-9" and tags.mb_release_id == "rel-1"
    # leerer Wert entfernt das Feld
    assert write_tags_any(str(flac), {"artist": ""})
    assert read_track_tags(str(flac)).artist == ""

    mp3 = make_album(tmp_path, "M", count=1) / "01.mp3"
    assert write_tags_any(str(mp3), {"title": "Neu", "track_number": 3, "total_tracks": 4}, {"X": "y"})
    meta = MetadataManager().read_metadata(str(mp3))
    assert meta.title == "Neu" and meta.track_number == 3 and meta.total_tracks == 4
    assert album_identity.read_txxx(str(mp3), "X") == "y"

    wav = tmp_path / "w.wav"
    subprocess.run([_ffmpeg(), "-loglevel", "error", "-f", "lavfi", "-i", "anullsrc=r=22050:cl=mono", "-t", "0.2",
                    str(wav)], check=True)
    assert write_tags_any(str(wav), {"title": "x"}) is False


def test_read_local_tracks_from_paths_and_apply_diff_flac(tmp_path):
    flac1 = make_flac(tmp_path / "Album" / "01.flac")
    flac2 = tmp_path / "Album" / "02.flac"
    shutil.copy(flac1, flac2)
    write_tags_any(str(flac1), {"title": "Eins", "album": "Alt", "track_number": 1})
    write_tags_any(str(flac2), {"title": "Zwei", "album": "Alt", "track_number": 2})

    local = tag_lookup.read_local_tracks([str(flac2), str(flac1)], MetadataManager())
    assert [t.index for t in local] == [1, 2] and local[0].meta.title == "Eins"

    candidate = tag_lookup.ReleaseCandidate(source=tag_lookup.SOURCE_MUSICBRAINZ, id="rel-1", title="Neu",
                                            artist="Preußler", date="2007", release_group_id="rg-1")
    candidate.tracks = [tag_lookup.ReleaseTrack(position=1, title="Eins neu", artist="Preußler", length=0.3),
                        tag_lookup.ReleaseTrack(position=2, title="Zwei neu", artist="Preußler", length=0.3)]
    candidate.track_count = 2
    diffs = tag_lookup.build_diff(local, candidate)
    assert all(d.matched for d in diffs)
    written = tag_lookup.apply_diff(diffs, {0: {tag_lookup.FIELD_TITLE, tag_lookup.FIELD_ALBUM,
                                                tag_lookup.FIELD_MB_IDS}, 1: {tag_lookup.FIELD_ALBUM}},
                                    MetadataManager())
    assert written == 2
    assert read_track_tags(str(flac1)).title == "Eins neu" and read_track_tags(str(flac1)).album == "Neu"
    assert read_track_tags(str(flac1)).mb_release_id == "rel-1"
    assert read_track_tags(str(flac2)).album == "Neu" and read_track_tags(str(flac2)).title == "Zwei"


def test_library_refresh_album_after_tagging(tmp_path):
    source = tmp_path / "Musik"
    folder = make_album(source, "Hexe", count=2, album="Alt", artist="Wer")
    lib = Library(str(tmp_path / "data"), [str(source)])
    index = lib.scan()
    assert index.albums[0].title == "Alt"
    for i in (1, 2):
        MetadataManager().write_metadata(str(folder / f"{i:02d}.mp3"), album="Die kleine Hexe", artist="Preußler")
    index = lib.refresh_album(str(folder), index)
    assert [a.title for a in index.albums] == ["Die kleine Hexe"]
    assert lib.load().albums[0].artist == "Preußler"


def test_tag_dialog_accepts_file_list(tmp_path, monkeypatch):
    pytest.importorskip("PyQt6")
    from PyQt6.QtCore import QCoreApplication, QSettings
    from PyQt6.QtWidgets import QApplication
    from gui.tag_diff_dialog import TagDiffDialog

    QCoreApplication.setOrganizationName("Tonuino-Manager-Tests")
    app = QApplication.instance() or QApplication([])
    monkeypatch.setattr(tag_lookup, "search_candidates", lambda *a, **k: [])  # kein Netz
    folder = make_album(tmp_path, "Hexe", count=2, album="Die kleine Hexe")
    files = [str(folder / "01.mp3"), str(folder / "02.mp3")]
    dialog = TagDiffDialog(files, MetadataManager(), None, "Die kleine Hexe", "Preußler", label="Hexe")
    assert dialog.windowTitle().startswith("Tags nachtragen – Hexe")
    for _ in range(50):  # die automatische Suche (QTimer) anlaufen und auslaufen lassen
        app.processEvents()
        if dialog._search_worker is not None:
            dialog._search_worker.wait(100)
    assert dialog.local is not None and [t.meta.title for t in dialog.local] == ["Track 1", "Track 2"]
    dialog._stop_workers()
    dialog.done(0)
    QSettings().clear()


def test_tag_writes_keep_id3v23_and_other_pictures(tmp_path):
    """Dateien der Sammlung bleiben ID3v2.3; beim Cover wird nur das Front-Cover
    ersetzt, Rueckseite und Booklet bleiben"""
    from mutagen.id3 import APIC, ID3
    from core.library import write_cover_any

    folder = make_album(tmp_path, "Album", count=1, album="Alt")
    path = str(folder / "01.mp3")
    tags = ID3(path)
    tags.add(APIC(encoding=3, mime="image/jpeg", type=4, desc="Rueckseite", data=b"\xff\xd8back"))
    tags.save(path, v2_version=3)
    assert ID3(path).version[:2] == (2, 3)

    assert write_tags_any(path, {"album": "Neu"})
    assert album_identity.write_album_id([path], "id-1")
    assert MetadataManager().write_metadata(path, title="Titel")
    assert write_cover_any(path, b"\xff\xd8front")

    tags = ID3(path)
    assert tags.version[:2] == (2, 3)
    pictures = {frame.type: frame.data for frame in tags.getall("APIC")}
    assert pictures == {3: b"\xff\xd8front", 4: b"\xff\xd8back"}


def test_disc_number_formats():
    from core.library import _number_pair
    assert _number_pair("2/3") == (2, 3)  # ID3 TPOS, Vorbis discnumber
    assert _number_pair(["1"]) == (1, 0)
    assert _number_pair([(1, 2)]) == (1, 2)  # MP4 disk
    assert _number_pair("") == (0, 0) and _number_pair(None) == (0, 0)
