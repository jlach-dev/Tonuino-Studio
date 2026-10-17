import io
import shutil
import subprocess

import pytest

pytest.importorskip("PyQt6")

from PIL import Image
from PyQt6.QtCore import QCoreApplication, QSettings
from PyQt6.QtWidgets import QApplication

from core import album_identity, card_store, cover_sources
from core.cover_archive import KIND_ONLINE, ROLE_ALBUM_COVER, ROLE_CARD_COVER, CoverArchive
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
def fake_sources(monkeypatch):
    """Deezer liefert ein Cover, iTunes dasselbe Bild (Duplikat) - ohne Netz"""
    calls = []

    def deezer(title, artist=""):
        calls.append(("Deezer", title, artist))
        return [cover_sources.OnlineCover("Deezer", "Folge 39", "Die Fuchsbande", "https://dz/500", "https://dz/1000")]

    def itunes(title, artist=""):
        return [cover_sources.OnlineCover("iTunes", "Folge 39", "Die Fuchsbande", "https://it/600", "https://it/1200")]

    monkeypatch.setattr(cover_sources, "SEARCHERS", {"Deezer": deezer, "iTunes": itunes})
    monkeypatch.setattr(cover_sources, "download_image",
                        lambda url, min_edge=250: png_bytes(color=(200, 50, 50) if "dz" in url or "it" in url else (0, 0, 0)))
    QSettings().setValue("covers/search_sources", ["Deezer", "iTunes"])
    return calls


def run_workers(app, dialog):
    for _ in range(3):
        for worker in (dialog._research_worker, dialog._search_worker, dialog._generate_worker):
            if worker is not None:
                worker.wait(5000)
        app.processEvents()


def section_count(dialog, section):
    return sum(1 for c in dialog.candidates if c.section == section)


def test_cover_manager_online_cover_as_card_and_album_cover(app, sd, tmp_path, monkeypatch, fake_sources):
    from gui import cover_manager_dialog as cm

    monkeypatch.setattr(cm, "gemini_client", lambda: None)
    archive = CoverArchive(str(tmp_path / "data"))
    folder = sd.folders[1]
    # "Ordner 01" ist nur der Anzeigename - darf nicht als Suchbegriff landen
    dialog = cm.CoverManagerDialog(folder, "Ordner 01", MetadataManager(), archive)
    dialog._start_research()
    run_workers(app, dialog)
    assert dialog.search_title_edit.text() != "Ordner 01"

    dialog.search_title_edit.setText("Fall 77: Die verschleppte Schleppe")
    dialog.search_artist_edit.setText("Die Fuchsbande")
    dialog._start_search()
    run_workers(app, dialog)

    assert ("Deezer", "Fall 77: Die verschleppte Schleppe", "Die Fuchsbande") in fake_sources
    assert section_count(dialog, cm.SECTION_ONLINE) == 1  # iTunes-Duplikat aussortiert
    assert dialog.selected is not None and dialog.selected.online.source == "Deezer"

    dialog.title_edit.setPlainText("Fall 77")
    dialog._set_card_cover()
    dialog._set_album_cover()

    meta = card_store.load_meta(folder.path)
    assert meta.title == "Fall 77"
    assert meta.album_id == dialog.album_id
    assert album_identity.read_album_id([folder.tracks[0].filepath]) == dialog.album_id
    assert MetadataManager().get_cover_bytes(folder.tracks[1].filepath)
    covers = archive.covers(dialog.album_id)
    assert len(covers) == 1
    assert covers[0].kind == KIND_ONLINE and covers[0].provider == "Deezer"
    assert covers[0].source == "https://dz/1000"  # volle Aufloesung archiviert
    assert set(covers[0].roles) == {ROLE_CARD_COVER, ROLE_ALBUM_COVER}
    assert section_count(dialog, cm.SECTION_ARCHIVE) == 1
    dialog.done(0)

    # zweites Oeffnen: Archiv-Cover und SD-Kartenmotiv sind wieder da
    dialog2 = cm.CoverManagerDialog(folder, "Ordner 01", MetadataManager(), archive)
    assert dialog2.album_id == meta.album_id
    assert section_count(dialog2, cm.SECTION_ARCHIVE) == 1
    assert section_count(dialog2, cm.SECTION_CURRENT) == 2
    dialog2.filter_bar.setCurrentIndex(3)  # Archiv
    visible = [dialog2.gallery.item(i) for i in range(dialog2.gallery.count())
               if not dialog2.gallery.item(i).isHidden()]
    assert len(visible) == 1
    run_workers(app, dialog2)
    dialog2.done(0)


def test_cover_manager_new_variants_need_keeping(app, sd, tmp_path, monkeypatch, fake_sources):
    from gui import cover_manager_dialog as cm

    monkeypatch.setattr(cm, "gemini_client", lambda: None)
    archive = CoverArchive(str(tmp_path / "data"))
    dialog = cm.CoverManagerDialog(sd.folders[1], "X", MetadataManager(), archive)
    run_workers(app, dialog)
    dialog._generation_meta = {"style": "Aquarell", "model": "m", "prompt": ""}
    dialog._on_variant_ready(Image.new("RGB", (400, 600), "green"))
    dialog._on_variant_ready(Image.new("RGB", (400, 600), "blue"))
    assert len(dialog.unsaved_variants()) == 2

    dialog._keep([c for c in dialog.candidates if c.section == cm.SECTION_NEW][0])
    assert len(dialog.unsaved_variants()) == 1
    assert [e.style for e in archive.covers(dialog.album_id)] == ["Aquarell"]
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


def test_cover_manager_marking_keep_reference_discard(app, sd, tmp_path, monkeypatch, fake_sources):
    from PyQt6.QtCore import Qt
    from gui import cover_manager_dialog as cm

    monkeypatch.setattr(cm, "gemini_client", lambda: None)
    archive = CoverArchive(str(tmp_path / "data"))
    dialog = cm.CoverManagerDialog(sd.folders[1], "X", MetadataManager(), archive)
    run_workers(app, dialog)
    dialog._generation_meta = {"style": "Aquarell", "model": "m", "prompt": ""}
    for color in ("green", "blue", "red"):
        dialog._on_variant_ready(Image.new("RGB", (400, 600), color))

    def mark(*indices):
        new = [c for c in dialog.candidates if c.section == cm.SECTION_NEW]
        for item in dialog._items():
            if item.data(Qt.ItemDataRole.UserRole) in [new[i] for i in indices]:
                item.setCheckState(Qt.CheckState.Checked)
        return [new[i] for i in indices]

    kept = mark(0, 1)
    assert dialog.btn_keep.text() == "Behalten (2)"
    dialog._keep_marked()
    assert len(archive.covers(dialog.album_id)) == 2
    assert all(c.kept for c in kept) and not dialog._marked_candidates()

    mark_items = [c for c in dialog.candidates if c.kept]
    for item in dialog._items():
        if item.data(Qt.ItemDataRole.UserRole) in mark_items:
            item.setCheckState(Qt.CheckState.Checked)
    dialog._toggle_reference_marked()
    assert len(dialog._references()) == 2
    assert "◆" in dialog._item_text(mark_items[0])

    monkeypatch.setattr(cm, "confirm_action", lambda *a, **k: True)
    for item in dialog._items():
        if item.data(Qt.ItemDataRole.UserRole) is mark_items[0]:
            item.setCheckState(Qt.CheckState.Checked)
    dialog._discard_marked()
    assert len(archive.covers(dialog.album_id)) == 1
    monkeypatch.setattr(dialog, "unsaved_variants", lambda: [])
    dialog.done(0)


def test_replacing_album_cover_archives_previous_and_refreshes(app, sd, tmp_path, monkeypatch, fake_sources):
    from gui import cover_manager_dialog as cm

    folder = sd.folders[1]
    for track in folder.tracks:
        MetadataManager().set_cover_bytes(track.filepath, png_bytes(color=(1, 2, 3)), "image/png")
    monkeypatch.setattr(cm, "gemini_client", lambda: None)
    archive = CoverArchive(str(tmp_path / "data"))
    dialog = cm.CoverManagerDialog(folder, "X", MetadataManager(), archive)
    run_workers(app, dialog)
    old = dialog._current_candidate("is_album_cover")
    assert old is not None

    asked = []
    monkeypatch.setattr(dialog, "_ask_archive_previous", lambda what: asked.append(what) or True)
    dialog._generation_meta = {"style": "Aquarell", "model": "m", "prompt": ""}
    dialog._on_variant_ready(Image.new("RGB", (500, 500), "green"))
    dialog._set_album_cover()

    assert asked == ["Album-Cover"]
    sources = sorted(e.source for e in archive.covers(dialog.album_id))
    assert "Früheres Album-Cover" in sources
    new_current = dialog._current_candidate("is_album_cover")
    assert new_current is not old and new_current.data == MetadataManager().get_cover_bytes(folder.tracks[0].filepath)

    # zweites Ersetzen: das jetzige Cover ist bereits archiviert -> keine Rueckfrage
    dialog._on_variant_ready(Image.new("RGB", (500, 500), "blue"))
    dialog._set_album_cover()
    assert asked == ["Album-Cover"]
    monkeypatch.setattr(dialog, "unsaved_variants", lambda: [])
    dialog.done(0)
