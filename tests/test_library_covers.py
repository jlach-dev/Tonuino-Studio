"""Album-Cover in der Bibliothek: alle Formate, Stapellauf aus dem Archiv, Cover-Manager im Bibliotheksmodus"""

import os
from pathlib import Path

import pytest

from core import album_identity
from core.cover_archive import KIND_FILE, ROLE_ALBUM_COVER, ROLE_CARD_COVER, CoverArchive
from core.library import LibraryAlbum, LibraryIndex, read_album, read_track_tags, write_album_cover, write_cover_any
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

    QSettings().setValue("library/write_id_tags", "no")  # ohne Erlaubnis bleiben die Originale ohne ID
    dialog._persist_album()
    assert album_identity.read_album_id([str(flac)]) is None
    QSettings().setValue("library/write_id_tags", "yes")

    dialog._set_album_cover()
    assert (folder_path / "cover.jpg").exists()
    for path in paths:
        assert MetadataManager().get_cover_bytes(path)
    assert album_identity.read_album_id([str(flac)]) == "lib1"
    QSettings().remove("library/write_id_tags")
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


def test_card_cover_role_error_is_reported(tmp_path, monkeypatch):
    """Scheitert das Vermerken der Rolle (covers.json gesperrt), steht card.png schon -
    der Nutzer erfaehrt, dass er erneut uebernehmen muss, statt eines Absturzdialogs"""
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
    archive.ensure_album("lib1", "Die kleine Hexe", "Preußler", path=str(folder_path))
    entry = archive.add_cover("lib1", png_bytes(size=(400, 400)), KIND_FILE, source="test")
    track = Track(index=1, filename="01.mp3", filepath=str(folder_path / "01.mp3"))
    folder = Folder(index=None, path=str(folder_path), tracks=[track], name="Hexe")
    dialog = cm.CoverManagerDialog(folder, "Die kleine Hexe", MetadataManager(), archive, focus="album",
                                   library=True, album_id="lib1")
    app.processEvents()
    dialog.selected = next(c for c in dialog.candidates if c.entry is not None and c.entry.id == entry.id)

    def locked(*_):
        raise PermissionError(5, "Zugriff verweigert")

    warnings = []
    monkeypatch.setattr(archive, "set_role", locked)
    monkeypatch.setattr(cm.QMessageBox, "warning", lambda _parent, title, text: warnings.append((title, text)))
    dialog._set_card_cover()
    assert card_store.has_card(str(archive.card_dir("lib1")))
    assert len(warnings) == 1 and "noch einmal" in warnings[0][1]
    assert ROLE_CARD_COVER not in archive.covers("lib1")[0].roles
    for worker in (dialog._research_worker, dialog._search_worker):
        if worker is not None:
            worker.wait(5000)
    app.processEvents()
    dialog.done(0)
    QSettings().clear()


class _Signal:
    def __init__(self):
        self.slots = []

    def connect(self, slot):
        self.slots.append(slot)

    def emit(self, *args):
        for slot in self.slots:
            slot(*args)


def _instant_worker(result, calls):
    """Ersatz fuer die KI-Worker: liefert sofort ein Bild und zaehlt die Aufrufe"""
    class Worker:
        def __init__(self, *_args):
            self.done, self.failed, self.finished = _Signal(), _Signal(), _Signal()

        def start(self):
            calls.append(1)
            self.done.emit(result())

        def isRunning(self):
            return False

        def wait(self, *_args):
            return True
    return Worker


def test_ai_back_survives_reopen_and_reaches_sd(tmp_path, monkeypatch):
    """Rand per KI -> Als Vorderseite, KI-Rueckseite -> Als Rueckseite -> schliessen -> oeffnen:
    beide Seiten sind da, nichts wird neu erzeugt, die Rueckseite haengt im Archiv am
    Kartencover und kommt beim Uebertragen mit auf die SD-Karte"""
    pytest.importorskip("PyQt6")
    from PIL import Image
    from PyQt6.QtCore import QCoreApplication, QSettings
    from PyQt6.QtWidgets import QApplication
    from core import card_store
    from core.card_layout import BACK_MODE_IMAGE
    from core.cover_archive import BACK_KIND_AI, KIND_ONLINE
    from core.library_import import ImportResult, _write_card
    from core.sd_card import Folder, Track
    from gui import cover_manager_dialog as cm

    QCoreApplication.setOrganizationName("Tonuino-Manager-Tests")
    app = QApplication.instance() or QApplication([])
    generated = []
    monkeypatch.setattr(cm, "CoverExtendWorker",
                        _instant_worker(lambda: Image.new("RGB", (900, 600), (10, 200, 10)), []))
    monkeypatch.setattr(cm, "CoverBackGenerateWorker",
                        _instant_worker(lambda: Image.new("RGB", (900, 600), (200, 10, 10)), generated))
    monkeypatch.setattr(cm, "gemini_client", lambda: object())
    monkeypatch.setattr(cm, "image_model", lambda: "m")
    monkeypatch.setattr(cm.CoverManagerDialog, "_start_research", lambda self: None)
    monkeypatch.setattr(cm.CoverManagerDialog, "unsaved_variants", lambda self: [])
    archive = CoverArchive(str(tmp_path / "data"))
    album_dir = make_album(tmp_path / "Musik", "Folge 58", count=1, album="Folge 58", artist="Bibi")
    folder = Folder(index=None, path=str(album_dir), name="Folge 58",
                    tracks=[Track(index=1, filename="01.mp3", filepath=str(album_dir / "01.mp3"))])

    def open_dialog():
        dialog = cm.CoverManagerDialog(folder, "Folge 58", MetadataManager(), archive, library=True,
                                       album_id="lib58")
        app.processEvents()
        return dialog

    def back_color(card_dir):
        with Image.open(card_store.back_paths(card_dir)[1]) as img:
            return img.convert("RGB").getpixel((img.width // 2, img.height // 2))

    dialog = open_dialog()
    dialog._add_candidate(cm.Candidate(cm.SECTION_ONLINE, "Spotify", png_bytes(size=(640, 640)), KIND_ONLINE),
                          select=True)
    dialog.btn_extend.click()          # neue Version "KI-Rand", angeklickt
    dialog.btn_card_cover.click()      # als Vorderseite -> gespeichert
    dialog.back_wish_edit.setText("Pferdehalfter auf Holzschemel")
    dialog.btn_ai_back.click()         # neues Bild "KI-Rückseite", angeklickt
    dialog.btn_back_cover.click()      # als Rueckseite -> gespeichert
    card_dir = dialog._card_dir
    assert len(generated) == 1 and card_store.has_back(card_dir) and back_color(card_dir) == (200, 10, 10)
    assert card_store.load_meta(card_dir).back_mode == BACK_MODE_IMAGE
    card_entry = next(e for e in archive.covers("lib58") if ROLE_CARD_COVER in e.roles)
    assert card_entry.back_file and card_entry.back_kind == BACK_KIND_AI
    dialog.done(0)

    dialog = open_dialog()
    assert dialog.front_source is not None and dialog.back_source is not None
    assert "gespeichert ✓" in dialog.sides_label.text()
    dialog.title_edit.setPlainText("Die Schmugglerpferde")
    dialog.done(0)  # Autosave beim Schliessen
    assert len(generated) == 1  # nicht neu erzeugt
    meta = card_store.load_meta(card_dir)
    assert meta.title == "Die Schmugglerpferde" and meta.archive_cover_id == card_entry.id
    assert back_color(card_dir) == (200, 10, 10)

    # Uebertragen auf die SD-Karte: die Rueckseite kommt mit
    sd_folder = Folder(index=1, path=str(tmp_path / "sd" / "01"), tracks=[], name="01")
    Path(sd_folder.path).mkdir(parents=True)
    result = ImportResult()
    _write_card(sd_folder, read_album(str(album_dir)), "lib58", archive, MetadataManager(), "landscape", result)
    assert result.card_written and result.back_written
    assert card_store.load_meta(sd_folder.path).back_mode == BACK_MODE_IMAGE
    QSettings().clear()


@pytest.mark.parametrize("library", [True, False], ids=["bibliothek", "sd"])
def test_pick_back_after_ai_back_with_real_buttons(tmp_path, monkeypatch, library):
    """Nur mit den Knoepfen unter der Vorschau: Vorderseite, Rueckseite, KI-Rueckseite und
    wieder ein Bild als Rueckseite - jede Wahl ist sofort gespeichert, auch wenn der
    Seiten-Umschalter zwischendurch auf "Rueckseite" stand"""
    pytest.importorskip("PyQt6")
    from PIL import Image
    from PyQt6.QtCore import QCoreApplication, QSettings
    from PyQt6.QtWidgets import QApplication
    from core import card_store
    from core.card_layout import BACK_MODE_IMAGE
    from core.cover_archive import KIND_ONLINE
    from core.sd_card import Folder, SDCard, Track
    from gui import cover_manager_dialog as cm

    QCoreApplication.setOrganizationName("Tonuino-Manager-Tests")
    app = QApplication.instance() or QApplication([])
    monkeypatch.setattr(cm, "CoverExtendWorker",
                        _instant_worker(lambda: Image.new("RGB", (600, 900), (10, 200, 10)), []))
    monkeypatch.setattr(cm, "CoverBackGenerateWorker",
                        _instant_worker(lambda: Image.new("RGB", (600, 900), (200, 10, 10)), []))
    monkeypatch.setattr(cm, "gemini_client", lambda: object())
    monkeypatch.setattr(cm, "image_model", lambda: "m")
    monkeypatch.setattr(cm.CoverManagerDialog, "_start_research", lambda self: None)
    monkeypatch.setattr(cm.CoverManagerDialog, "unsaved_variants", lambda self: [])
    archive = CoverArchive(str(tmp_path / "data"))
    if library:
        album_dir = make_album(tmp_path / "Musik", "A", count=1, album="Folge 58", artist="Bibi")
        folder = Folder(index=None, path=str(album_dir), name="A",
                        tracks=[Track(index=1, filename="01.mp3", filepath=str(album_dir / "01.mp3"))])
        dialog = cm.CoverManagerDialog(folder, "Folge 58", MetadataManager(), archive, library=True,
                                       album_id="libA")
    else:
        make_album(tmp_path / "sd", "01", count=1, album="Folge 58", artist="Bibi")
        (tmp_path / "sd" / "01" / "01.mp3").rename(tmp_path / "sd" / "01" / "001.mp3")
        sd = SDCard(str(tmp_path / "sd"))
        sd.scan()
        dialog = cm.CoverManagerDialog(sd.folders[1], "Folge 58", MetadataManager(), archive)
    yellow, red = (230, 220, 0), (200, 10, 10)
    front = cm.Candidate(cm.SECTION_ONLINE, "Front", png_bytes(size=(640, 640), color=(0, 0, 200)), KIND_ONLINE)
    back = cm.Candidate(cm.SECTION_ONLINE, "Back", png_bytes(size=(640, 640), color=yellow), KIND_ONLINE)
    dialog._add_candidate(front)
    dialog._add_candidate(back)

    def click(candidate):
        row = next(r for r in range(dialog.gallery.count()) if dialog.gallery.item(r).data(256) is candidate)
        dialog.gallery.setCurrentRow(row)
        app.processEvents()

    def saved_back_color():
        with Image.open(card_store.back_paths(dialog._card_dir)[1]) as img:
            return img.convert("RGB").getpixel((img.width // 2, img.height // 2))

    dialog.tabs.setCurrentIndex(0)
    click(front)
    dialog.btn_card_cover.click()
    click(back)
    dialog.btn_back_cover.click()
    assert saved_back_color() == yellow

    dialog.btn_ai_back.click()      # neues Bild, angeklickt
    dialog.btn_back_cover.click()
    assert saved_back_color() == red

    dialog.side_bar.setCurrentIndex(1)  # Umschalter bleibt auf "Rueckseite" stehen
    click(back)
    assert dialog.btn_back_cover.isEnabled() and dialog.btn_back_cover.text() == "Als Rückseite"
    dialog.btn_back_cover.click()
    assert card_store.load_meta(dialog._card_dir).back_mode == BACK_MODE_IMAGE
    assert saved_back_color() == yellow
    dialog.done(0)
    QSettings().clear()