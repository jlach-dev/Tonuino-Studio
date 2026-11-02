import io
import os
import shutil
import subprocess
from pathlib import Path

import pytest
from PIL import Image

from core import album_identity, card_store, library
from core.cover_archive import ROLE_ALBUM_COVER, ROLE_CARD_COVER, KIND_AI, AlbumRecord, CoverArchive
from core.library import (
    STATE_LINKED, STATE_MOVED, STATE_NEW, STATE_OFFLINE, Library, read_album, read_track_tags,
)
from core.metadata import MetadataManager
from core.sd_card import SDCard


def _ffmpeg():
    import imageio_ffmpeg
    return imageio_ffmpeg.get_ffmpeg_exe()


def make_silent(path, seconds=0.3, codec_args=("-q:a", "9")):
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run([_ffmpeg(), "-loglevel", "error", "-f", "lavfi", "-i", "anullsrc=r=22050:cl=mono",
                    "-t", str(seconds), *codec_args, str(path)], check=True)
    return path


def make_album(root, name, count=2, album="", artist="", title_prefix="Track"):
    folder = root / name
    first = make_silent(folder / "01.mp3")
    for i in range(2, count + 1):
        shutil.copy(first, folder / f"{i:02d}.mp3")
    manager = MetadataManager()
    for i in range(1, count + 1):
        manager.write_metadata(str(folder / f"{i:02d}.mp3"), album=album or name, artist=artist or "Interpret",
                               title=f"{title_prefix} {i}", track_number=i)
    return folder


def png_bytes(size=(80, 80), color=(200, 30, 30)):
    buffer = io.BytesIO()
    Image.new("RGB", size, color).save(buffer, "PNG")
    return buffer.getvalue()


# --- Scan ---------------------------------------------------------------------------------------

def test_scan_finds_albums_merges_discs_and_reads_tags(tmp_path):
    source = tmp_path / "Musik"
    make_album(source, "Hexe", count=3, album="Die kleine Hexe", artist="Preußler")
    MetadataManager().set_cover_bytes(str(source / "Hexe" / "01.mp3"), png_bytes(), "image/png")
    # Doppel-CD: Unterordner CD1/CD2 ergeben ein Album
    make_album(source / "Räuber", "CD1", count=2, album="Räuber Hotzenplotz")
    make_album(source / "Räuber", "CD2", count=1, album="Räuber Hotzenplotz")
    (source / "leer").mkdir()
    (source / "Hexe" / "notes.txt").write_text("x")

    lib = Library(str(tmp_path / "data"), [str(source)])
    index = lib.scan()

    names = sorted(a.name for a in index.albums)
    assert names == ["Die kleine Hexe", "Räuber Hotzenplotz"]
    hexe = next(a for a in index.albums if a.title == "Die kleine Hexe")
    assert hexe.artist == "Preußler" and hexe.track_count == 3 and hexe.duration > 0.5
    assert hexe.has_cover and lib.thumb_path(hexe).is_file()
    assert hexe.fingerprint and hexe.state == STATE_NEW
    raeuber = next(a for a in index.albums if a.title == "Räuber Hotzenplotz")
    assert raeuber.track_count == 3 and raeuber.path == str(source / "Räuber")
    assert [p.split("CD")[-1][:1] for p in raeuber.tracks] == ["1", "1", "2"]


def test_scan_is_incremental_and_index_roundtrip(tmp_path, monkeypatch):
    source = tmp_path / "Musik"
    make_album(source, "A", count=1)
    lib = Library(str(tmp_path / "data"), [str(source)])
    first = lib.scan()
    lib.save(first)

    loaded = lib.load()
    assert loaded.scanned == first.scanned
    assert [a.path for a in loaded.albums] == [a.path for a in first.albums]
    assert loaded.albums[0].tracks == first.albums[0].tracks

    calls = []
    original = library.read_album
    monkeypatch.setattr(library, "read_album", lambda *a, **k: calls.append(a) or original(*a, **k))
    second = lib.scan(loaded)
    assert calls == []  # unveraendert: aus dem Cache uebernommen
    make_album(source, "B", count=1)
    third = lib.scan(second)
    assert len(calls) == 1 and len(third.albums) == 2


def test_offline_source_keeps_cached_albums(tmp_path):
    source = tmp_path / "NAS"
    make_album(source, "A", count=1)
    lib = Library(str(tmp_path / "data"), [str(source)])
    index = lib.scan()
    shutil.rmtree(source)

    again = lib.scan(index)
    assert len(again.albums) == 1 and again.albums[0].offline
    assert again.albums[0].state == STATE_OFFLINE
    archive = CoverArchive(str(tmp_path / "data"))
    assert lib.match(again, archive).albums[0].state == STATE_OFFLINE


def test_match_states_linked_moved_new_orphaned(tmp_path):
    source = tmp_path / "Musik"
    linked = make_album(source, "Linked", count=1)
    moved = make_album(source, "Moved", count=1)
    make_album(source, "New", count=1)
    album_identity.write_album_id([str(linked / "01.mp3")], "id-linked")
    album_identity.write_album_id([str(moved / "01.mp3")], "id-moved")

    archive = CoverArchive(str(tmp_path / "data"))
    archive.ensure_album("id-linked", "Linked", path=str(linked))
    archive.ensure_album("id-moved", "Moved", path=str(source / "alter Ort"))
    archive.ensure_album("id-gone", "Weg", path=str(source / "Verschwunden"))
    archive.ensure_album("id-sd-only", "Nur SD", path="E:/01")

    lib = Library(str(tmp_path / "data"), [str(source)])
    index = lib.match(lib.scan(), archive)
    states = {a.name: a.state for a in index.albums}
    assert states == {"Linked": STATE_LINKED, "Moved": STATE_MOVED, "New": STATE_NEW}
    assert archive.load_album("id-moved").paths == [str(moved)]  # alter Pfad entfernt, neuer eingetragen
    assert [o["id"] for o in index.orphaned] == ["id-gone"]


def test_match_by_fingerprint_without_tag(tmp_path):
    source = tmp_path / "Musik"
    make_album(source, "Kopie", count=2)
    lib = Library(str(tmp_path / "data"), [str(source)])
    index = lib.scan()
    archive = CoverArchive(str(tmp_path / "data"))
    archive.save_album(AlbumRecord(id="id-fp", title="Kopie", fingerprint=index.albums[0].fingerprint,
                                   paths=[str(source / "Kopie")]))
    index = lib.match(index, archive)
    assert index.albums[0].state == STATE_LINKED and index.albums[0].album_id == "id-fp"


def test_read_tags_flac_and_write_id_skips_wav(tmp_path):
    flac = make_silent(tmp_path / "a.flac", codec_args=("-c:a", "flac"))
    from mutagen.flac import FLAC
    audio = FLAC(str(flac))
    audio["album"], audio["artist"], audio["musicbrainz_albumid"] = ["Flac-Album"], ["Flac-Artist"], ["mb-1"]
    audio.save()
    tags = read_track_tags(str(flac))
    assert (tags.album, tags.artist, tags.mb_release_id) == ("Flac-Album", "Flac-Artist", "mb-1")
    assert library.write_album_id_any(str(flac), "id-flac")
    assert read_track_tags(str(flac)).album_id == "id-flac"

    wav = make_silent(tmp_path / "b.wav", codec_args=())
    assert not library.write_album_id_any(str(wav), "id-wav")
    album = read_album(str(tmp_path))
    assert album.track_count == 2 and album.album_id == "id-flac"


def test_sd_album_ids_from_card_json_and_tags(tmp_path):
    sd_root = tmp_path / "sd"
    make_album(sd_root, "01", count=1)
    make_album(sd_root, "02", count=1)
    album_identity.write_album_id([str(sd_root / "01" / "01.mp3")], "id-a")
    for folder in ("01", "02"):
        for i, f in enumerate(sorted((sd_root / folder).glob("*.mp3")), start=1):
            f.rename(sd_root / folder / f"{i:03d}.mp3")
    card_store._save_meta(str(sd_root / "02"), card_store.CardMeta(album_id="id-b"))
    card = SDCard(str(sd_root))
    card.scan()
    assert library.sd_album_ids(card.folders.values()) == {"id-a": 1, "id-b": 2}


# --- Import (Drop) -----------------------------------------------------------------------------

@pytest.fixture
def sd(tmp_path):
    (tmp_path / "sd").mkdir()
    card = SDCard(str(tmp_path / "sd"))
    card.scan()
    return card


def test_import_album_copies_tags_links_and_writes_card(tmp_path, sd):
    from core.audio_converter import AudioConverter
    from core.library_import import MODE_REPLACE, import_album

    source = tmp_path / "Musik"
    album_dir = make_album(source, "Hexe", count=2, album="Die kleine Hexe", artist="Preußler")
    archive = CoverArchive(str(tmp_path / "data"))
    lib = Library(str(tmp_path / "data"), [str(source)])
    album = lib.scan().albums[0]
    # Archiv kennt das Album schon (ueber den Titel) und hat Album- und Kartencover mit Rueckseite
    archive.ensure_album("id-hexe", "Die kleine Hexe", "Preußler", path="E:/05")
    album_cover = archive.add_cover("id-hexe", png_bytes(color=(0, 0, 200)), KIND_AI)
    archive.set_role("id-hexe", album_cover.id, ROLE_ALBUM_COVER)
    card_cover = archive.add_cover("id-hexe", Image.new("RGB", (600, 900), (10, 200, 10)), KIND_AI, style="Aquarell")
    archive.set_role("id-hexe", card_cover.id, ROLE_CARD_COVER)
    archive.set_back("id-hexe", card_cover.id, png_bytes((600, 900)))

    folder = sd.create_folder(1)
    manager = MetadataManager()
    result = import_album(sd, folder, album, AudioConverter(), manager, archive, write_library_tags=True)

    assert result.errors == [] and result.added == 2 and result.album_id == "id-hexe"
    assert sorted(p.name for p in (sd.path / "01").glob("*.mp3")) == ["001.mp3", "002.mp3"]
    assert album_identity.read_album_id([str(sd.path / "01" / "001.mp3")]) == "id-hexe"
    assert result.library_tagged == 2
    assert album_identity.read_album_id([str(album_dir / "01.mp3")]) == "id-hexe"
    record = archive.load_album("id-hexe")
    assert str(album_dir) in record.paths and folder.path in record.paths and record.fingerprint == album.fingerprint
    assert result.cover_embedded == 2 and manager.get_cover_bytes(str(sd.path / "01" / "001.mp3"))
    with Image.open(io.BytesIO(manager.get_cover_bytes(str(sd.path / "01" / "001.mp3")))) as embedded:
        assert embedded.format == "JPEG" and max(embedded.size) <= 600  # nicht das PNG aus dem Archiv
    assert result.card_written and result.back_written
    assert card_store.has_card(folder.path) and card_store.has_back(folder.path)
    meta = card_store.load_meta(folder.path)
    assert meta.album_id == "id-hexe" and meta.archive_cover_id == card_cover.id and meta.title

    # Ersetzen: alte Tracks weg, neue wieder ab 001
    result = import_album(sd, folder, album, AudioConverter(), manager, archive, write_library_tags=False,
                          mode=MODE_REPLACE)
    assert result.added == 2 and len(folder.tracks) == 2
    assert sorted(p.name for p in (sd.path / "01").glob("*.mp3")) == ["001.mp3", "002.mp3"]
    assert [t.filepath for t in folder.tracks] == [str(sd.path / "01" / "001.mp3"), str(sd.path / "01" / "002.mp3")]
    assert not (sd.path / "01" / ".tmp_import").exists() and card_store.has_card(folder.path)


    # Anhaengen eines anderen Albums: die vorhandenen Tracks behalten ihre Album-ID,
    # Karte und Rueckseite des Ordners bleiben, nur die neuen Tracks werden getaggt
    other_dir = make_album(source, "Andere", count=1, album="Anderes Album", artist="Jemand")
    other = Library(str(tmp_path / "data"), [str(source)]).scan().find(str(other_dir))
    card_before = (Path(folder.path) / "card.png").read_bytes()
    result = import_album(sd, folder, other, AudioConverter(), manager, archive, write_library_tags=False)
    assert result.added == 1 and result.card_kept and not result.card_written
    assert album_identity.read_album_id([str(sd.path / "01" / "001.mp3")]) == "id-hexe"
    assert album_identity.read_album_id([str(sd.path / "01" / "003.mp3")]) == result.album_id != "id-hexe"
    assert (Path(folder.path) / "card.png").read_bytes() == card_before and card_store.has_back(folder.path)
    assert "Kartencover des Ordners beibehalten" in result.summary


def test_replace_keeps_old_tracks_when_nothing_was_copied(tmp_path, sd):
    from core.audio_converter import AudioConverter
    from core.library_import import MODE_REPLACE, import_album

    sd.create_folder(2)
    for name in ("001.mp3", "002.mp3"):
        (sd.path / "02" / name).write_bytes(name.encode())
    sd.scan()
    folder = sd.folders[2]
    album = read_album(str(make_album(tmp_path / "Musik", "Neu", count=1)))

    # Alle Quelldateien unlesbar: Kopieren scheitert komplett
    result = import_album(sd, folder, album, AudioConverter(), MetadataManager(), None, write_library_tags=False,
                          mode=MODE_REPLACE, tracks=[str(tmp_path / "fehlt.mp3")])

    assert result.added == 0 and result.errors
    assert sorted(p.name for p in (sd.path / "02").iterdir()) == ["001.mp3", "002.mp3"]
    assert (sd.path / "02" / "001.mp3").read_bytes() == b"001.mp3"
    assert [t.filename for t in folder.tracks] == ["001.mp3", "002.mp3"]


def test_staged_replace_discard_and_commit(tmp_path, sd):
    from core import library_import
    from core.sd_card import Track

    sd.create_folder(4)
    for name in ("001.mp3", "002.mp3", "003.mp3"):
        (sd.path / "04" / name).write_bytes(b"alt")
    sd.scan()
    folder = sd.folders[4]

    # Abgebrochen: Zwischenordner weg, alte Tracks unveraendert
    staging = library_import.create_staging_folder(folder)
    (Path(staging.path) / "001.mp3").write_bytes(b"neu")
    library_import.discard_staging_folder(staging)
    assert sorted(p.name for p in (sd.path / "04").iterdir()) == ["001.mp3", "002.mp3", "003.mp3"]

    # Leerer Zwischenordner: nichts ersetzen
    staging = library_import.create_staging_folder(folder)
    assert library_import.commit_staged_replace(sd, folder, staging) is False
    assert len(folder.tracks) == 3 and not Path(staging.path).exists()

    # Zwei neue Tracks ersetzen drei alte
    staging = library_import.create_staging_folder(folder)
    for index in (1, 2):
        path = Path(staging.path) / f"{index:03d}.mp3"
        path.write_bytes(b"neu%d" % index)
        staging.tracks.append(Track(index=index, filename=path.name, filepath=str(path)))
    assert library_import.commit_staged_replace(sd, folder, staging) is True
    assert sorted(p.name for p in (sd.path / "04").iterdir()) == ["001.mp3", "002.mp3"]
    assert (sd.path / "04" / "002.mp3").read_bytes() == b"neu2"
    assert [t.filepath for t in folder.tracks] == [str(sd.path / "04" / "001.mp3"), str(sd.path / "04" / "002.mp3")]


def test_import_without_archive_and_without_permission_leaves_library_untouched(tmp_path, sd):
    from core.audio_converter import AudioConverter
    from core.library_import import import_album

    album_dir = make_album(tmp_path / "Musik", "Neu", count=1)
    album = read_album(str(album_dir))
    folder = sd.create_folder(3)
    result = import_album(sd, folder, album, AudioConverter(), MetadataManager(), None, write_library_tags=False)
    assert result.added == 1 and result.album_id and result.library_tagged == 0
    assert album_identity.read_album_id([str(album_dir / "01.mp3")]) is None
    assert album_identity.read_album_id([str(sd.path / "03" / "001.mp3")]) == result.album_id
    assert not result.card_written


def test_scan_keeps_albums_of_unreadable_folders(tmp_path, monkeypatch):
    """NAS-Aussetzer mitten im Einlesen: Alben unter einem nicht lesbaren Ordner
    bleiben als offline im Index, statt zu verschwinden"""
    from core import library as library_module
    source = tmp_path / "Musik"
    make_album(source / "A", "Eins", count=1, album="Eins")
    make_album(source / "B", "Zwei", count=1, album="Zwei")
    lib = Library(str(tmp_path / "data"), [str(source)])
    first = lib.scan()
    assert len(first.albums) == 2

    real_walk = library_module.os.walk

    def flaky_walk(top, onerror=None, **kwargs):
        for dirpath, dirnames, filenames in real_walk(top, onerror=onerror, **kwargs):
            if os.path.basename(dirpath) == "B":
                onerror(OSError(5, "Netzwerkname nicht mehr verfügbar", dirpath))
                dirnames[:] = []
                continue
            yield dirpath, dirnames, filenames

    monkeypatch.setattr(library_module.os, "walk", flaky_walk)
    second = lib.scan(first)
    names = {a.name: a.offline for a in second.albums}
    assert names == {"Eins": False, "Zwei": True}
    assert second.unreadable and second.unreadable[0].endswith("B")


def test_album_id_is_not_taken_from_weak_matches(tmp_path):
    from core.cover_archive import AlbumRecord, CoverArchive
    from core.library import LibraryAlbum
    from core.library_import import resolve_album_id

    archive = CoverArchive(str(tmp_path))
    archive.save_album(AlbumRecord(id="bibi", title="Folge 1", artist="Bibi", fingerprint="fp-single"))
    archive.save_album(AlbumRecord(id="zwei", title="Doppel", artist="X", fingerprint="fp-zwei"))

    # gleicher Titel, aber ohne Interpret: anderes Album
    assert resolve_album_id(LibraryAlbum(path="a", title="Folge 1"), archive) != "bibi"
    assert resolve_album_id(LibraryAlbum(path="a", title="Folge 1", artist="bibi"), archive) == "bibi"
    # Einzelner Track: gleiche Laenge beweist nichts, ab zwei Tracks gilt der Fingerabdruck
    single = LibraryAlbum(path="s", fingerprint="fp-single", tracks=["1.mp3"])
    assert resolve_album_id(single, archive) != "bibi"
    double = LibraryAlbum(path="d", fingerprint="fp-zwei", tracks=["1.mp3", "2.mp3"])
    assert resolve_album_id(double, archive) == "zwei"


def test_update_check_without_any_release_is_no_failure(monkeypatch):
    import io
    import urllib.error
    from core import updater

    def not_found(request, timeout=0):
        raise urllib.error.HTTPError(updater.GITHUB_API_URL, 404, "Not Found", {}, io.BytesIO())

    monkeypatch.setattr(updater.urllib.request, "urlopen", not_found)
    checker = updater.UpdateChecker("2.0.0")
    results = []
    checker.no_update.connect(lambda: results.append("none"))
    checker.check_failed.connect(lambda message: results.append(message))
    checker.run()
    assert results == ["none"]
