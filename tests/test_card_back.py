import base64
import io
from types import SimpleNamespace

import pytest
from PIL import Image

from core import card_store, cover_sources, musicbrainz
from core.card_layout import (
    BACK_MODE_BLUR, BACK_MODE_GRADIENT, BACK_MODE_IMAGE, BACK_MODE_TRACKLIST, CardSpec,
    ORIENTATION_LANDSCAPE, ORIENTATION_PORTRAIT, blur_background, gradient_background, render_back_card,
)
from core.cover_archive import KIND_ONLINE, ROLE_CARD_COVER, CoverArchive
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
    """Als Vorderseite / Als Rueckseite speichern sofort; Klicks in der Galerie aendern nichts;
    "Rand per KI" legt nur eine neue Version an, zugewiesen wird per Knopf"""
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

    dialog = cm.CoverManagerDialog(sd.folders[1], "Eule", MetadataManager(), CoverArchive(str(tmp_path / "data")))
    card_dir = sd.folders[1].path

    with_back = cm.Candidate(cm.SECTION_ONLINE, "MB", png_bytes(color=(10, 10, 10)), KIND_ONLINE,
                             back_data=png_bytes(color=(200, 0, 0)))
    other = cm.Candidate(cm.SECTION_ONLINE, "Deezer", png_bytes(color=(0, 0, 200)), KIND_ONLINE)
    dialog._add_candidate(with_back, select=True)
    dialog._add_candidate(other)
    assert not card_store.has_card(card_dir)

    assert blur_background(Image.new("RGB", (300, 300), "red"), (100, 150)).size == (100, 150)
    assert gradient_background(Image.new("RGB", (300, 300), (200, 30, 30)), (100, 150)).size == (100, 150)

    # Rueckseite von "MB" als Vorderseite: sofort gespeichert, ohne "Uebernehmen"
    dialog.side_bar.setCurrentIndex(1)
    assert dialog._preview_source().back
    dialog._pick_source(back=False)
    assert dialog.front_source.candidate is with_back and dialog.front_source.back
    assert dialog._front_image().getpixel((0, 0)) == (200, 0, 0)
    assert card_store.has_card(card_dir)
    assert "gespeichert ✓" in dialog.sides_label.text()

    # anderes Bild -> "Als Rueckseite": Modus "Bild", sofort gespeichert
    dialog.gallery.setCurrentRow(1)
    dialog.side_bar.setCurrentIndex(0)
    dialog._pick_source(back=True)
    assert dialog.back_source.candidate is other and not dialog.back_source.back
    assert dialog.back_mode_combo.currentData() == BACK_MODE_IMAGE
    assert card_store.has_back(card_dir) and card_store.load_meta(card_dir).back_mode == BACK_MODE_IMAGE
    assert "Rückseite: Deezer (Vorderseite)" in dialog.sides_label.toolTip()
    assert dialog.slot_back.caption_text == "Deezer (Vorderseite)"

    # Klick auf ein anderes Cover (auch mit eigener Rueckseite) aendert die Karte nicht
    dialog.gallery.setCurrentRow(0)
    assert dialog.back_source.candidate is other and dialog.front_source.candidate is with_back
    dialog.side_bar.setCurrentIndex(1)
    assert dialog._preview_source().candidate is with_back and dialog._preview_source().back
    dialog.tabs.setCurrentIndex(1)  # Reiter Karte: die Vorschau zeigt die Karte (festgelegte Rueckseite)
    assert dialog._preview_source().candidate is other
    dialog.tabs.setCurrentIndex(0)
    # Moduswechsel verwirft die gewaehlte Rueckseite nicht und springt nicht um
    cm._select_data(dialog.back_mode_combo, BACK_MODE_BLUR)
    assert dialog.back_source.candidate is other and dialog.side_bar.currentIndex() == 1
    dialog.side_bar.setCurrentIndex(0)
    cm._select_data(dialog.back_mode_combo, "")
    dialog._render_preview()
    assert dialog.preview.pixmap() is not None and not dialog.preview.pixmap().isNull()

    # "Rand per KI ergaenzen" am quadratischen Bild: neue Version, nicht zugewiesen
    class FakeSignal:
        def connect(self, slot):
            pass

    class FakeWorker:
        done = failed = refused = finished = doubtful = FakeSignal()

        def __init__(self, client, model, data, orientation):
            assert data == other.data

        def start(self):
            dialog._on_extended(Image.new("RGB", (400, 600), (1, 2, 3)))

        def isRunning(self):
            return False

    monkeypatch.setattr(cm, "CoverExtendWorker", lambda *a: FakeWorker(*a))
    dialog.gallery.setCurrentRow(1)
    dialog.side_bar.setCurrentIndex(0)
    dialog.btn_extend.click()
    new = [c for c in dialog.candidates if c.section == cm.SECTION_NEW]
    assert len(new) == 1 and new[0].kind == "ai" and new[0].image().size == (400, 600)
    assert dialog.selected is new[0]
    assert dialog.front_source.candidate is with_back  # noch nicht zugewiesen
    assert other.back_data is None and with_back.data == png_bytes(color=(10, 10, 10))  # Originale unveraendert
    dialog.btn_card_cover.click()
    assert dialog.front_source.candidate is new[0] and new[0].kept
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
            output_image=image, output_text='{"front": "Eine Eule mit Gitarre", '
                                            '"ideas": ["Fritzi im Wald", "Die Lupe", "Der Bär am Fluss"]}')))
    art = generate_back_art(client, "m", req, png_bytes(), "", True)
    assert art.size == (400, 600)
    assert calls[0]["input"][1]["mime_type"] == "image/png"
    assert suggest_back_ideas(client, "t", req) == ["Fritzi im Wald", "Die Lupe", "Der Bär am Fluss"]
    assert isinstance(calls[-1]["input"], str)  # ohne Vorderseite: nur Text
    suggest_back_ideas(client, "t", req, png_bytes())
    assert calls[-1]["input"][0]["mime_type"] == "image/png"  # Bild zuerst, dann die Aufgabe
    assert "Das Bild hat Vorrang" in calls[-1]["input"][1]["text"]
    assert suggest_back_ideas.last_front_description == "Eine Eule mit Gitarre"


def test_ai_back_button_creates_image_to_assign(qt_app, tmp_path, monkeypatch):
    """"KI-Rueckseite erzeugen" legt ein eigenes Bild an; "Als Rueckseite" speichert es sofort"""
    from core.cover_archive import BACK_KIND_AI
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

    archive = CoverArchive(str(tmp_path / "data"))
    dialog = cm.CoverManagerDialog(sd.folders[1], "X", MetadataManager(), archive)
    card_dir = sd.folders[1].path
    front = cm.Candidate(cm.SECTION_ONLINE, "Deezer", png_bytes(color=(0, 0, 200)), KIND_ONLINE)
    dialog._add_candidate(front, select=True)
    assert not dialog.btn_ai_back.isEnabled()  # erst mit Vorderseite
    dialog.btn_card_cover.click()
    assert dialog.btn_ai_back.isEnabled()
    dialog.back_tracklist_check.setChecked(True)

    class FakeSignal:
        def connect(self, slot):
            pass

    class FakeBackWorker:
        done = failed = refused = finished = FakeSignal()

        def __init__(self, client, model, request, front_bytes, wish, with_tracklist, masking=None):
            assert with_tracklist and front_bytes == front.data and wish == "Fritzi im Wald"

        def start(self):
            dialog._on_back_generated(Image.new("RGB", (400, 600), (9, 9, 9)))

        def isRunning(self):
            return False

    monkeypatch.setattr(cm, "CoverBackGenerateWorker", lambda *a: FakeBackWorker(*a))
    dialog.back_wish_edit.setText("Fritzi im Wald")
    dialog._ai_research_done = True
    dialog.btn_ai_back.click()

    new = [c for c in dialog.candidates if c.section == cm.SECTION_NEW]
    assert len(new) == 1 and new[0].meta["kind"] == "ai_back" and not new[0].has_back
    assert new[0].image().size == (400, 600) and dialog.selected is new[0]
    assert not card_store.has_back(card_dir)  # erzeugt, aber noch nicht zugewiesen

    dialog.btn_back_cover.click()
    meta = card_store.load_meta(card_dir)
    assert meta.back_mode == BACK_MODE_IMAGE and meta.back_tracklist and meta.back_prompt == "Fritzi im Wald"
    assert card_store.has_back(card_dir) and new[0].kept
    card_entry = next(e for e in archive.covers(dialog.album_id) if ROLE_CARD_COVER in e.roles)
    assert card_entry.back_file and card_entry.back_kind == BACK_KIND_AI
    dialog.done(0)


def test_text_changes_save_automatically(qt_app, tmp_path, monkeypatch):
    """Titel und Layout speichern kurz nach der Eingabe; Schliessen speichert eine anstehende Aenderung"""
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

    archive = CoverArchive(str(tmp_path / "data"))
    dialog = cm.CoverManagerDialog(sd.folders[1], "X", MetadataManager(), archive)
    card_dir = sd.folders[1].path
    dialog.title_edit.setPlainText("Ohne Vorderseite")
    assert dialog._autosave_timer is None and not card_store.has_card(card_dir)  # nichts zu speichern

    front = cm.Candidate(cm.SECTION_ONLINE, "Deezer", png_bytes(color=(0, 0, 200)), KIND_ONLINE)
    dialog._add_candidate(front, select=True)
    dialog.btn_card_cover.click()
    dialog.title_edit.setPlainText("Heia Walpurgisnacht")
    assert dialog._autosave_timer.isActive() and "wird gespeichert" in dialog.sides_label.text()
    dialog._autosave_timer.stop()
    dialog._autosave()
    assert card_store.load_meta(card_dir).title == "Heia Walpurgisnacht"
    assert "gespeichert ✓" in dialog.sides_label.text()

    cm._select_data(dialog.back_mode_combo, BACK_MODE_BLUR)  # Rueckseitenmodus: ebenfalls Autosave
    dialog.series_edit.setText("Die kleine Hexe")
    dialog.done(0)  # anstehende Aenderung wird beim Schliessen gespeichert
    meta = card_store.load_meta(card_dir)
    assert meta.series == "Die kleine Hexe" and meta.back_mode == BACK_MODE_BLUR and card_store.has_back(card_dir)

    # Wiederoeffnen: die gespeicherte Karte ist Vorder- und Rueckseite, kein Autosave beim Laden
    again = cm.CoverManagerDialog(sd.folders[1], "X", MetadataManager(), archive)
    assert again.front_source is not None and again.front_source.candidate.is_sd_card_art
    assert again._autosave_timer is None
    assert "gespeichert ✓" in again.sides_label.text()
    again.done(0)


def test_extension_skipped_when_art_already_fits(tmp_path, monkeypatch):
    pytest.importorskip("PyQt6")
    from PyQt6.QtWidgets import QApplication
    from core.metadata import MetadataManager
    from gui import cover_manager_dialog as cm

    app = QApplication.instance() or QApplication([])
    assert app is not None

    folder = tmp_path / "sd" / "01"
    folder.mkdir(parents=True)
    (folder / "001.mp3").write_bytes(b"x")
    sd = SDCard(str(tmp_path / "sd"))
    sd.scan()
    monkeypatch.setattr(cm, "gemini_client", lambda: object())
    monkeypatch.setattr(cm, "image_model", lambda: "m")
    monkeypatch.setattr(cm.CoverManagerDialog, "_start_research", lambda self: None)

    dialog = cm.CoverManagerDialog(sd.folders[1], "Eule", MetadataManager(), CoverArchive(str(tmp_path / "data")))
    cm._select_data(dialog.orientation_combo, ORIENTATION_PORTRAIT)
    square = cm.Candidate(cm.SECTION_ONLINE, "quadratisch", png_bytes(), KIND_ONLINE)
    tall = cm.Candidate(cm.SECTION_ONLINE, "hochkant", png_bytes(size=(400, 600)), KIND_ONLINE)
    dialog._add_candidate(square, select=True)
    dialog._add_candidate(tall)

    calls = []

    class FakeSignal:
        def connect(self, slot):
            pass

    class FakeWorker:
        done = failed = refused = finished = doubtful = FakeSignal()

        def __init__(self, *args):
            calls.append(args)

        def start(self):
            pass

        def isRunning(self):
            return False

    monkeypatch.setattr(cm, "CoverExtendWorker", FakeWorker)
    dialog.gallery.setCurrentRow(1)
    assert dialog.selected is tall
    dialog.btn_extend.click()
    assert calls == [] and "keine Randergänzung nötig" in dialog.status_label.text()
    dialog.gallery.setCurrentRow(0)
    dialog.btn_extend.click()
    assert len(calls) == 1  # quadratisch: Aufruf noetig
    monkeypatch.setattr(dialog, "unsaved_variants", lambda: [])
    dialog.done(0)


def test_keep_ai_back_version_with_ai_front(tmp_path, monkeypatch):
    """Absturz beim Behalten: meta["kind"] = "ai_back" kollidierte mit dem Archiv-Parameter kind"""
    pytest.importorskip("PyQt6")
    from PyQt6.QtWidgets import QApplication
    from core.cover_archive import KIND_AI
    from core.metadata import MetadataManager
    from gui import cover_manager_dialog as cm

    app = QApplication.instance() or QApplication([])
    assert app is not None
    folder = tmp_path / "sd" / "01"
    folder.mkdir(parents=True)
    (folder / "001.mp3").write_bytes(b"x")
    sd = SDCard(str(tmp_path / "sd"))
    sd.scan()
    monkeypatch.setattr(cm, "gemini_client", lambda: object())
    monkeypatch.setattr(cm.CoverManagerDialog, "_start_research", lambda self: None)
    archive = CoverArchive(str(tmp_path / "data"))
    dialog = cm.CoverManagerDialog(sd.folders[1], "Eule", MetadataManager(), archive)
    version = cm.Candidate(cm.SECTION_NEW, "KI-Rückseite · Front #1", png_bytes(), KIND_AI,
                           meta={"kind": "ai_back", "style": "Aquarell", "model": "m", "prompt": "p"},
                           back_data=png_bytes(color=(9, 9, 9)))
    dialog._add_candidate(version, select=True)
    from PyQt6.QtCore import Qt
    item = next(i for i in dialog._items() if i.data(Qt.ItemDataRole.UserRole) is version)
    item.setCheckState(Qt.CheckState.Checked)
    dialog._keep_marked()
    entries = archive.covers(dialog.album_id)
    assert len(entries) == 1 and entries[0].kind == KIND_AI and entries[0].style == "Aquarell"
    assert entries[0].back_file and version.entry.id == entries[0].id
    monkeypatch.setattr(dialog, "unsaved_variants", lambda: [])
    dialog.done(0)


def test_ai_back_survives_gallery_clicks(tmp_path, monkeypatch):
    """Die zugewiesene KI-Rueckseite bleibt die Rueckseite, egal welches Bild angeklickt wird"""
    pytest.importorskip("PyQt6")
    from PyQt6.QtWidgets import QApplication
    from core.metadata import MetadataManager
    from gui import cover_manager_dialog as cm

    app = QApplication.instance() or QApplication([])
    assert app is not None

    folder = tmp_path / "sd" / "01"
    folder.mkdir(parents=True)
    (folder / "001.mp3").write_bytes(b"x")
    sd = SDCard(str(tmp_path / "sd"))
    sd.scan()
    monkeypatch.setattr(cm, "gemini_client", lambda: object())
    monkeypatch.setattr(cm, "image_model", lambda: "m")
    monkeypatch.setattr(cm.CoverManagerDialog, "_start_research", lambda self: None)

    dialog = cm.CoverManagerDialog(sd.folders[1], "Eule", MetadataManager(), CoverArchive(str(tmp_path / "data")))
    card_dir = sd.folders[1].path
    front = cm.Candidate(cm.SECTION_ONLINE, "Deezer", png_bytes(color=(0, 0, 200)), KIND_ONLINE)
    with_back = cm.Candidate(cm.SECTION_ONLINE, "MB", png_bytes(), KIND_ONLINE, back_data=png_bytes(color=(9, 9, 9)))
    dialog._add_candidate(front, select=True)
    dialog._add_candidate(with_back)
    dialog._pick_source(back=False)

    dialog._back_front = dialog.front_source
    dialog._on_back_generated(Image.new("RGB", (400, 600), (7, 7, 7)))
    ai_back = dialog.selected
    dialog._pick_source(back=True)
    assert dialog.back_source.candidate is ai_back
    dialog.gallery.setCurrentRow(1)  # Cover mit eigener Rueckseite anklicken
    assert dialog.selected is with_back and dialog.back_source.candidate is ai_back
    assert dialog._back_source_image(dialog.selected).getpixel((0, 0)) == (7, 7, 7)
    assert ai_back.label in dialog.sides_label.toolTip()
    with Image.open(card_store.back_paths(card_dir)[1]) as img:
        assert img.convert("RGB").getpixel((img.width // 2, img.height // 2)) == (7, 7, 7)
    dialog.done(0)

def test_old_card_with_ai_back_mode_loads_as_image(qt_app, tmp_path, monkeypatch):
    """Karten aus der Zeit mit Rueckseitenmodus "KI" behalten ihre gespeicherte Rueckseite"""
    from core.card_layout import BACK_MODE_AI
    from core.metadata import MetadataManager
    from gui import cover_manager_dialog as cm

    folder = tmp_path / "sd" / "01"
    folder.mkdir(parents=True)
    (folder / "001.mp3").write_bytes(b"x")
    sd = SDCard(str(tmp_path / "sd"))
    sd.scan()
    monkeypatch.setattr(cm, "gemini_client", lambda: None)
    monkeypatch.setattr(cm.CoverManagerDialog, "_start_research", lambda self: None)
    meta = card_store.CardMeta(title="Alt", orientation=ORIENTATION_PORTRAIT, back_mode=BACK_MODE_AI)
    card_store.save_card(str(folder), Image.new("RGB", (400, 600), (0, 0, 200)), meta, 1)
    card_store.save_back(str(folder), Image.new("RGB", (400, 600), (7, 7, 7)), card_store.load_meta(str(folder)))
    dialog = cm.CoverManagerDialog(sd.folders[1], "Alt", MetadataManager(), CoverArchive(str(tmp_path / "data")))
    assert dialog.back_mode_combo.currentData() == BACK_MODE_IMAGE
    assert dialog.back_source is not None and dialog.back_source.back
    assert dialog._back_source_image(dialog.selected).getpixel((0, 0)) == (7, 7, 7)
    dialog.done(0)


def test_closing_with_remembered_back_and_no_front_asks_first(qt_app, tmp_path, monkeypatch):
    """Ohne Vorderseite ist die gewaehlte Rueckseite nur gemerkt, Schliessen wuerde sie verlieren"""
    from core.metadata import MetadataManager
    from gui import cover_manager_dialog as cm

    folder = tmp_path / "sd" / "01"
    folder.mkdir(parents=True)
    (folder / "001.mp3").write_bytes(b"x")
    sd = SDCard(str(tmp_path / "sd"))
    sd.scan()
    monkeypatch.setattr(cm.CoverManagerDialog, "_start_research", lambda self: None)
    dialog = cm.CoverManagerDialog(sd.folders[1], "Eule", MetadataManager(), CoverArchive(str(tmp_path / "data")))
    dialog._add_candidate(cm.Candidate(cm.SECTION_ONLINE, "Deezer", png_bytes(), KIND_ONLINE), select=True)
    dialog._pick_source(back=True)
    assert dialog.front_source is None and dialog.back_source is not None

    shown = []

    class Box(cm.QMessageBox):
        answer = "Zurück"

        def exec(self):
            shown.append(self.text())
            self.answer_button = next(b for b in self.buttons() if b.text() == Box.answer)
            return 0

        def clickedButton(self):
            return self.answer_button

    monkeypatch.setattr(cm, "QMessageBox", Box)
    dialog.show()
    dialog.done(0)
    assert len(shown) == 1 and "noch nicht gespeichert" in shown[0]
    assert dialog.isVisible()  # "Zurueck" laesst den Dialog offen

    Box.answer = "Verwerfen"
    dialog.done(0)
    assert not dialog.isVisible()
