import base64
import io
from types import SimpleNamespace

import pytest
from PIL import Image

from core import card_store, cover_sources, musicbrainz
from core.card_layout import (
    BACK_MODE_AI, BACK_MODE_BLUR, BACK_MODE_GRADIENT, BACK_MODE_IMAGE, BACK_MODE_TRACKLIST, CardSpec,
    ORIENTATION_LANDSCAPE, ORIENTATION_PORTRAIT, blur_background, gradient_background, render_back_card,
)
from core.cover_archive import KIND_ONLINE, CoverArchive
from core.cover_generator import GenerationRequest, extend_to_card
from core.sd_card import SDCard


def png_bytes(size=(300, 300), color=(0, 120, 255)):
    buffer = io.BytesIO()
    Image.new("RGB", size, color).save(buffer, "PNG")
    return buffer.getvalue()


def test_render_back_card_sizes_and_text():
    art = Image.new("RGB", (600, 600), (30, 30, 30))
    for orientation in (ORIENTATION_PORTRAIT, ORIENTATION_LANDSCAPE):
        plain = render_back_card(art, orientation)
        listed = render_back_card(art, orientation, "Die kleine Hexe", [f"{i:02d}. Titel {i}" for i in range(1, 30)])
        assert plain.size == listed.size == CardSpec().size_px(orientation)
        # Textfeld macht das Bild oben dunkler
        assert listed.getpixel((listed.width // 2, listed.height // 6)) != plain.getpixel((plain.width // 2, plain.height // 6))


def test_card_store_back_roundtrip_and_purge(tmp_path):
    folder = tmp_path / "01"
    folder.mkdir()
    (folder / "001.mp3").write_bytes(b"x")
    meta = card_store.CardMeta(title="X", back_mode=BACK_MODE_TRACKLIST, back_heading="X", back_lines=["01. A"])
    card_store.save_card(str(folder), Image.new("RGB", (400, 600)), meta, 1)
    card_store.save_back(str(folder), Image.new("RGB", (600, 600), "red"), meta)

    assert card_store.has_back(str(folder))
    assert card_store.load_meta(str(folder)).back_mode == BACK_MODE_TRACKLIST
    assert card_store.load_back_raw(str(folder)).size == (600, 600)

    card = SDCard(str(tmp_path))
    card.scan()
    assert card.preview_purge().foreign_items_in_folders == 0
    card.purge()
    assert card_store.has_back(str(folder))

    card_store.delete_back(str(folder), card_store.load_meta(str(folder)))
    assert not card_store.has_back(str(folder))
    assert card_store.load_meta(str(folder)).back_mode == ""
    card_store.save_back(str(folder), Image.new("RGB", (600, 600)), meta)
    card_store.delete_card(str(folder))
    assert sorted(p.name for p in folder.iterdir()) == ["001.mp3"]


def test_archive_back_attach_and_delete(tmp_path):
    archive = CoverArchive(str(tmp_path))
    archive.ensure_album("a", "X")
    entry = archive.add_cover("a", png_bytes(), KIND_ONLINE, source="https://x/1")
    assert archive.load_back_bytes("a", entry) is None
    updated = archive.set_back("a", entry.id, png_bytes(color=(9, 9, 9)))
    assert updated.back_file.endswith("_back.png")
    assert archive.load_back_bytes("a", archive.covers("a")[0]) == png_bytes(color=(9, 9, 9))
    archive.set_back("a", entry.id, None)
    assert archive.covers("a")[0].back_file == ""
    archive.set_back("a", entry.id, png_bytes())
    archive.delete_cover("a", entry.id)
    assert not list((tmp_path / "albums" / "a" / "covers").iterdir())


def test_musicbrainz_source_reports_back_cover(monkeypatch):
    hit = musicbrainz.ReleaseGroupHit("rg", "Eule findet den Beat", "Eule", "2014", 100)
    images = [musicbrainz.CoverArtImage(hit, "2", "b500", "b1200", False, ["Back"]),
              musicbrainz.CoverArtImage(hit, "1", "f500", "f1200", True, ["Front"])]
    monkeypatch.setattr(musicbrainz, "search_release_groups", lambda t, a="": [hit])
    monkeypatch.setattr(musicbrainz, "list_cover_art", lambda g: images)
    covers = cover_sources.search_musicbrainz("Eule findet den Beat")
    assert len(covers) == 1
    assert covers[0].preview_url == "f500" and covers[0].back_preview_url == "b500"
    assert covers[0].back_full_url == "b1200"

    backs = []
    monkeypatch.setattr(cover_sources, "SEARCHERS", {"MusicBrainz": lambda t, a="": covers})
    monkeypatch.setattr(cover_sources, "download_image",
                        lambda url, min_edge=250: png_bytes(color=(1, 1, 1) if url == "b500" else (2, 2, 2)))
    cover_sources.search_all("x", "", ["MusicBrainz"], on_back=lambda c, d: backs.append(d))
    assert backs == [png_bytes(color=(1, 1, 1))]


def test_extend_to_card_requests_outpainting():
    calls = []
    image = SimpleNamespace(data=base64.b64encode(png_bytes((400, 600))).decode())
    client = SimpleNamespace(interactions=SimpleNamespace(
        create=lambda **kw: calls.append(kw) or SimpleNamespace(output_image=image)))
    result = extend_to_card(client, "m", png_bytes(), ORIENTATION_PORTRAIT)
    assert result.size == (400, 600)
    assert calls[0]["response_format"]["aspect_ratio"] == "2:3"
    assert "unchanged" in calls[0]["input"][0]["text"]
    assert calls[0]["input"][1]["mime_type"] == "image/png"


@pytest.fixture
def qt_app():
    pytest.importorskip("PyQt6")
    from PyQt6.QtCore import QCoreApplication, QSettings
    from PyQt6.QtWidgets import QApplication
    QCoreApplication.setOrganizationName("Tonuino-Manager-Tests")
    QCoreApplication.setApplicationName("tests")
    app = QApplication.instance() or QApplication([])
    yield app
    QSettings().clear()


def test_cover_manager_saves_back_side(qt_app, tmp_path, monkeypatch):
    from core.metadata import MetadataManager
    from gui import cover_manager_dialog as cm
    from gui.print_sheet_dialog import PrintSheetDialog

    folder = tmp_path / "sd" / "01"
    folder.mkdir(parents=True)
    (folder / "001.mp3").write_bytes(b"x")
    sd = SDCard(str(tmp_path / "sd"))
    sd.scan()
    monkeypatch.setattr(cm, "gemini_client", lambda: None)
    monkeypatch.setattr(cm.CoverManagerDialog, "_start_research", lambda self: None)
    dialog = cm.CoverManagerDialog(sd.folders[1], "Eule findet den Beat", MetadataManager(),
                                   CoverArchive(str(tmp_path / "data")))

    front = cm.Candidate(cm.SECTION_ONLINE, "MusicBrainz", png_bytes(), KIND_ONLINE,
                         online=cover_sources.OnlineCover("MusicBrainz", "Eule", "", "f", "f", "", "b", "b"),
                         back_data=png_bytes(color=(200, 0, 0)))
    dialog._add_candidate(front, select=True)
    assert "⧉" in dialog._item_text(front)

    cm._select_data(dialog.back_mode_combo, BACK_MODE_IMAGE)
    dialog.side_bar.setCurrentIndex(1)
    dialog._render_preview()
    assert dialog.preview.pixmap() is not None and not dialog.preview.pixmap().isNull()

    dialog._set_card_cover()
    assert card_store.has_card(sd.folders[1].path) and card_store.has_back(sd.folders[1].path)
    # "Aktuell" zeigt das neue Kartenmotiv sofort mit Rueckseite (kein Neuladen noetig)
    assert dialog._current_candidate("is_sd_card_art").has_back
    meta = card_store.load_meta(sd.folders[1].path)
    assert meta.back_mode == BACK_MODE_IMAGE
    assert dialog.archive.covers(dialog.album_id)[0].back_file  # Rueckseite archiviert

    # Trackliste auf dem Ambient Blur eines Bildes ohne eigene Rueckseite
    plain = cm.Candidate(cm.SECTION_NEW, "KI", png_bytes(color=(0, 200, 0)), "ai")
    dialog._add_candidate(plain, select=True)
    cm._select_data(dialog.back_mode_combo, BACK_MODE_BLUR)
    dialog.back_tracklist_check.setChecked(True)
    dialog.back_lines_edit.setPlainText("01. Titel-Song\n02. Rock-Song")
    dialog._set_card_cover()
    saved_meta = card_store.load_meta(sd.folders[1].path)
    assert saved_meta.back_lines == ["01. Titel-Song", "02. Rock-Song"] and saved_meta.back_tracklist
    assert saved_meta.back_mode == BACK_MODE_BLUR

    # Farbverlauf
    cm._select_data(dialog.back_mode_combo, BACK_MODE_GRADIENT)
    dialog._set_card_cover()
    assert card_store.load_meta(sd.folders[1].path).back_mode == BACK_MODE_GRADIENT
    assert card_store.has_back(sd.folders[1].path)

    # Druckbogen: Vorderseiten, dann Rueckseiten
    sd.folders[1].card_path = str(card_store.card_paths(sd.folders[1].path)[0])
    sheet = PrintSheetDialog([sd.folders[1]], {1: "Eule"})
    assert sheet.backs_check.isChecked()
    sheet.copies_spin.setValue(2)
    assert len(sheet._items()) == 4
    sheet.backs_check.setChecked(False)
    assert len(sheet._items()) == 2

    cm._select_data(dialog.back_mode_combo, "")
    dialog._set_card_cover()
    assert not card_store.has_back(sd.folders[1].path)
    monkeypatch.setattr(dialog, "unsaved_variants", lambda: [])
    dialog.done(0)


def test_preview_source_pick_and_ai_extension_creates_new_version(qt_app, tmp_path, monkeypatch):
    from core.metadata import MetadataManager
    from gui import cover_manager_dialog as cm

    folder = tmp_path / "sd" / "01"
    folder.mkdir(parents=True)
    (folder / "001.mp3").write_bytes(b"x")
    sd = SDCard(str(tmp_path / "sd"))
    sd.scan()
    monkeypatch.setattr(cm, "gemini_client", lambda: object())
    monkeypatch.setattr(cm.CoverManagerDialog, "_start_research", lambda self: None)
    dialog = cm.CoverManagerDialog(sd.folders[1], "Eule", MetadataManager(), CoverArchive(str(tmp_path / "data")))

    with_back = cm.Candidate(cm.SECTION_ONLINE, "MB", png_bytes(color=(10, 10, 10)), KIND_ONLINE,
                             back_data=png_bytes(color=(200, 0, 0)))
    other = cm.Candidate(cm.SECTION_ONLINE, "Deezer", png_bytes(color=(0, 0, 200)), KIND_ONLINE)
    dialog._add_candidate(with_back, select=True)
    dialog._add_candidate(other)
    cm._select_data(dialog.back_mode_combo, BACK_MODE_IMAGE)

    assert blur_background(Image.new("RGB", (300, 300), "red"), (100, 150)).size == (100, 150)
    assert gradient_background(Image.new("RGB", (300, 300), (200, 30, 30)), (100, 150)).size == (100, 150)

    # Vorschau zeigt die Rueckseite von "MB" -> als Vorderseiten-Motiv uebernehmen
    dialog.side_bar.setCurrentIndex(1)
    assert dialog._preview_source().back
    dialog._pick_source(back=False)
    assert dialog.front_source.candidate is with_back and dialog.front_source.back
    assert dialog._front_image().getpixel((0, 0)) == (200, 0, 0)

    # anderes Bild anklicken, dessen Vorderseite als Rueckseite waehlen
    dialog.gallery.setCurrentRow(1)
    dialog.side_bar.setCurrentIndex(0)
    dialog._pick_source(back=True)
    assert dialog.back_source.candidate is other and not dialog.back_source.back
    assert dialog._back_source_image(other).getpixel((0, 0)) == (0, 0, 200)

    # Klick auf ein Cover mit eigener Rueckseite zeigt dessen Rueckseite, nicht die zuvor gewaehlte
    dialog.gallery.setCurrentRow(0)
    assert dialog.back_source is None
    dialog.side_bar.setCurrentIndex(1)
    assert dialog._preview_source().candidate is with_back and dialog._preview_source().back
    cm._select_data(dialog.back_mode_combo, "")
    dialog._render_preview()
    assert dialog.preview.pixmap() is not None and not dialog.preview.pixmap().isNull()  # auch ohne aktiven Modus
    cm._select_data(dialog.back_mode_combo, BACK_MODE_IMAGE)
    dialog.gallery.setCurrentRow(1)
    dialog.side_bar.setCurrentIndex(0)
    dialog._pick_source(back=True)
    assert dialog.back_source.candidate is other

    # KI-Randergaenzung der Rueckseite: neue Version unter "Neu", Original unveraendert
    class FakeSignal:
        def connect(self, slot):
            pass

    class FakeWorker:
        done = failed = finished = FakeSignal()

        def __init__(self, client, model, data, orientation):
            self.data = data

        def start(self):
            dialog._on_extended(Image.new("RGB", (400, 600), (1, 2, 3)))

        def isRunning(self):
            return False

    monkeypatch.setattr(cm, "CoverExtendWorker", lambda *a: FakeWorker(*a))
    monkeypatch.setattr(cm, "image_model", lambda: "m")
    saved = []
    monkeypatch.setattr(dialog, "_set_card_cover", lambda: saved.append(True))

    # "Uebernehmen" fuehrt beide angehakten Randergaenzungen als Batch aus und speichert danach
    dialog.front_extend_check.setChecked(True)
    dialog.back_extend_check.setChecked(True)
    assert "Rand der Vorderseite ergänzen, Rand der Rückseite ergänzen" in dialog.apply_note.text()
    dialog._apply()

    new = [c for c in dialog.candidates if c.section == cm.SECTION_NEW]
    assert len(new) == 2
    front_version = next(c for c in new if c.kind == "ai")
    back_version = next(c for c in new if c.kind != "ai")
    assert front_version.image().size == (400, 600)
    assert back_version.back_image().size == (400, 600)
    assert back_version.image().getpixel((0, 0)) == (0, 0, 200)  # Vorderseite der Quelle uebernommen
    assert other.back_data is None and with_back.data == png_bytes(color=(10, 10, 10))  # Originale unveraendert
    assert dialog.front_source.candidate is front_version and dialog.back_source.candidate is back_version
    assert saved == [True]
    assert not dialog.front_extend_check.isChecked() and not dialog.back_extend_check.isChecked()

    # ohne Haken speichert "Uebernehmen" direkt
    dialog._apply()
    assert saved == [True, True]
    monkeypatch.setattr(dialog, "unsaved_variants", lambda: [])
    dialog.done(0)


def test_back_prompt_and_ideas():
    from types import SimpleNamespace
    from core.cover_generator import build_back_prompt, generate_back_art, suggest_back_ideas
    from core.cover_research import CoverBrief, FolderInfo

    info = FolderInfo(album="Folge 39: Fall 77: Die verschleppte Schleppe/Fall 78: Der gefiederte Vergessling",
                      artist="Die Fuchsbande")
    req = GenerationRequest(info=info, brief=CoverBrief(), style="Aquarell")
    prompt = build_back_prompt(req, with_tracklist=True)
    assert "BACK side" in prompt and "FRONT side" in prompt
    assert "Fall 78: Der gefiederte Vergessling" in prompt  # zweite Folge als Szene
    assert "text panel" in prompt and "no text" in prompt
    wished = build_back_prompt(req, "Fritzi mit Lupe im Wald", with_tracklist=False)
    assert "Fritzi mit Lupe im Wald" in wished and "full-bleed" in wished

    calls = []
    image = SimpleNamespace(data=base64.b64encode(png_bytes((400, 600))).decode())
    client = SimpleNamespace(interactions=SimpleNamespace(
        create=lambda **kw: calls.append(kw) or SimpleNamespace(
            output_image=image, output_text='["Fritzi im Wald", "Die Lupe", "Der Bär am Fluss"]')))
    art = generate_back_art(client, "m", req, png_bytes(), "", True)
    assert art.size == (400, 600)
    assert calls[0]["input"][1]["mime_type"] == "image/png"
    assert suggest_back_ideas(client, "t", req) == ["Fritzi im Wald", "Die Lupe", "Der Bär am Fluss"]


def test_ai_back_is_generated_on_apply_and_becomes_new_version(qt_app, tmp_path, monkeypatch):
    from core.metadata import MetadataManager
    from gui import cover_manager_dialog as cm

    folder = tmp_path / "sd" / "01"
    folder.mkdir(parents=True)
    (folder / "001.mp3").write_bytes(b"x")
    sd = SDCard(str(tmp_path / "sd"))
    sd.scan()
    monkeypatch.setattr(cm, "gemini_client", lambda: object())
    monkeypatch.setattr(cm, "image_model", lambda: "m")
    monkeypatch.setattr(cm.CoverManagerDialog, "_start_research", lambda self: None)
    dialog = cm.CoverManagerDialog(sd.folders[1], "X", MetadataManager(), CoverArchive(str(tmp_path / "data")))
    front = cm.Candidate(cm.SECTION_ONLINE, "Deezer", png_bytes(color=(0, 0, 200)), KIND_ONLINE)
    dialog._add_candidate(front, select=True)
    cm._select_data(dialog.back_mode_combo, BACK_MODE_AI)
    dialog.back_tracklist_check.setChecked(True)
    assert dialog._needs_ai_back()
    assert "KI-Rückseite erzeugen" in dialog.apply_note.text()

    class FakeSignal:
        def connect(self, slot):
            pass

    class FakeBackWorker:
        done = failed = finished = FakeSignal()

        def __init__(self, client, model, request, front_bytes, wish, with_tracklist):
            assert with_tracklist and front_bytes == front.data
            self.wish = wish

        def start(self):
            dialog._on_back_generated(Image.new("RGB", (400, 600), (9, 9, 9)))

        def isRunning(self):
            return False

    monkeypatch.setattr(cm, "CoverBackGenerateWorker", lambda *a: FakeBackWorker(*a))
    dialog.back_wish_edit.setText("Fritzi im Wald")
    dialog._apply()

    new = [c for c in dialog.candidates if c.section == cm.SECTION_NEW]
    assert len(new) == 1 and new[0].meta["kind"] == "ai_back" and new[0].back_image().size == (400, 600)
    assert not dialog._needs_ai_back()
    meta = card_store.load_meta(sd.folders[1].path)
    assert meta.back_mode == BACK_MODE_AI and meta.back_tracklist and meta.back_prompt == "Fritzi im Wald"
    assert card_store.has_back(sd.folders[1].path)

    # erneutes Uebernehmen erzeugt nicht noch einmal
    monkeypatch.setattr(cm, "CoverBackGenerateWorker", lambda *a: pytest.fail("nicht erneut erzeugen"))
    dialog._apply()
    monkeypatch.setattr(dialog, "unsaved_variants", lambda: [])
    dialog.done(0)
