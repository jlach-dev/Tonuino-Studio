import io
import shutil
import subprocess

import pytest

pytest.importorskip("PyQt6")

from PIL import Image
from PyQt6.QtCore import QCoreApplication, QSettings
from PyQt6.QtWidgets import QApplication

from core import album_identity, card_store, musicbrainz
from core.cover_archive import ROLE_ALBUM_COVER, ROLE_CARD_COVER, CoverArchive
from core.metadata import MetadataManager
from core.sd_card import SDCard


@pytest.fixture(scope="module")
def app():
    QCoreApplication.setOrganizationName("Tonuino-Manager-Tests")
    QCoreApplication.setApplicationName("tests")
    application = QApplication.instance() or QApplication([])
    yield application
    QSettings().clear()


@pytest.fixture
def sd(tmp_path):
    import imageio_ffmpeg
    folder = tmp_path / "sd" / "01"
    folder.mkdir(parents=True)
    subprocess.run([imageio_ffmpeg.get_ffmpeg_exe(), "-loglevel", "error", "-f", "lavfi", "-i",
                    "anullsrc=r=22050:cl=mono", "-t", "0.3", "-q:a", "9", str(folder / "001.mp3")], check=True)
    shutil.copy(folder / "001.mp3", folder / "002.mp3")
    card = SDCard(str(tmp_path / "sd"))
    card.scan()
    return card


def png_bytes(size=(300, 300), color=(0, 120, 255)):
    buffer = io.BytesIO()
    Image.new("RGB", size, color).save(buffer, "PNG")
    return buffer.getvalue()


@pytest.fixture
def fake_mb(monkeypatch):
    hit = musicbrainz.ReleaseGroupHit("rg1", "Die kleine Hexe", "Preußler", "2007", 100)
    image = musicbrainz.CoverArtImage(hit, "1", "https://caa/1-500.jpg", "https://caa/1-1200.jpg", True)
    monkeypatch.setattr(musicbrainz, "search_release_groups", lambda album, artist="": [hit])
    monkeypatch.setattr(musicbrainz, "list_cover_art", lambda group: [image])
    monkeypatch.setattr(musicbrainz, "fetch_image", lambda url: png_bytes(color=(200, 50, 50)))


def run_workers(app, dialog):
    for _ in range(3):
        for worker in (dialog._research_worker, dialog._mb_worker, dialog._generate_worker):
            if worker is not None:
                worker.wait(5000)
        app.processEvents()


def test_cover_manager_mb_cover_as_card_and_album_cover(app, sd, tmp_path, monkeypatch, fake_mb):
    from gui import cover_manager_dialog

    monkeypatch.setattr(cover_manager_dialog, "gemini_client", lambda: None)
    archive = CoverArchive(str(tmp_path / "data"))
    folder = sd.folders[1]
    dialog = cover_manager_dialog.CoverManagerDialog(folder, "Die kleine Hexe", MetadataManager(), archive)
    dialog._start_research()
    run_workers(app, dialog)
    dialog.mb_album_edit.setText("Die kleine Hexe")
    dialog._start_mb_search()
    run_workers(app, dialog)

    mb_list = dialog.lists[cover_manager_dialog.SECTION_MB]
    assert mb_list.count() == 1
    mb_list.setCurrentRow(0)
    assert dialog.selected.mb_image is not None

    dialog.title_edit.setText("Die kleine Hexe")
    dialog._set_card_cover()
    dialog._set_album_cover()

    # Karte auf der SD-Karte, verknuepft mit dem Archiv
    meta = card_store.load_meta(folder.path)
    assert meta.title == "Die kleine Hexe"
    assert meta.album_id == dialog.album_id
    # Tracks tragen die Album-ID, Cover ist eingebettet
    assert album_identity.read_album_id([folder.tracks[0].filepath]) == dialog.album_id
    assert MetadataManager().get_cover_bytes(folder.tracks[1].filepath)
    # Archiv: ein MusicBrainz-Cover mit beiden Rollen
    covers = archive.covers(dialog.album_id)
    assert len(covers) == 1
    assert set(covers[0].roles) == {ROLE_CARD_COVER, ROLE_ALBUM_COVER}
    dialog.done(0)

    # zweites Oeffnen: Archiv-Cover und SD-Kartenmotiv sind wieder da
    dialog2 = cover_manager_dialog.CoverManagerDialog(folder, "Die kleine Hexe", MetadataManager(), archive)
    assert dialog2.album_id == meta.album_id
    assert dialog2.lists[cover_manager_dialog.SECTION_ARCHIVE].count() == 1
    assert dialog2.lists[cover_manager_dialog.SECTION_CURRENT].count() == 2
    run_workers(app, dialog2)
    dialog2.done(0)


def test_cover_manager_new_variants_need_keeping(app, sd, tmp_path, monkeypatch, fake_mb):
    from gui import cover_manager_dialog

    monkeypatch.setattr(cover_manager_dialog, "gemini_client", lambda: None)
    archive = CoverArchive(str(tmp_path / "data"))
    dialog = cover_manager_dialog.CoverManagerDialog(sd.folders[1], "X", MetadataManager(), archive)
    run_workers(app, dialog)
    dialog._generation_meta = {"style": "Aquarell", "model": "m", "prompt": ""}
    dialog._on_variant_ready(Image.new("RGB", (400, 600), "green"))
    dialog._on_variant_ready(Image.new("RGB", (400, 600), "blue"))
    assert len(dialog.unsaved_variants()) == 2

    dialog._keep(dialog.candidates[cover_manager_dialog.SECTION_NEW][0])
    assert len(dialog.unsaved_variants()) == 1
    assert [e.style for e in archive.covers(dialog.album_id)] == ["Aquarell"]
    # nur eine Variante behalten -> verwerfen beim Schliessen
    monkeypatch.setattr(dialog, "unsaved_variants", lambda: [])
    dialog.done(0)


def test_print_sheet_dialog_renders_items(app, sd):
    from gui.print_sheet_dialog import PrintSheetDialog

    folder = sd.folders[1]
    card_store.save_card(folder.path, Image.new("RGB", (600, 900), "red"), card_store.CardMeta(title="X"), 1)
    folder.card_path = str(card_store.card_paths(folder.path)[0])

    dialog = PrintSheetDialog([folder], {1: "X"})
    dialog.copies_spin.setValue(3)
    assert len(dialog._items()) == 3
