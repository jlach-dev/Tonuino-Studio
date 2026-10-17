import io
import shutil
import subprocess
from types import SimpleNamespace

import pytest

pytest.importorskip("PyQt6")

from PIL import Image
from PyQt6.QtCore import QCoreApplication, QSettings
from PyQt6.QtWidgets import QApplication

from core.cover_archive import ROLE_SERIES_ANCHOR, CoverArchive
from core.metadata import MetadataManager
from core.sd_card import SDCard
from core.series import SIDE_FRONT


@pytest.fixture(scope="module")
def app():
    QCoreApplication.setOrganizationName("Tonuino-Manager-Tests")
    QCoreApplication.setApplicationName("tests-series")
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
    MetadataManager().write_metadata(str(folder / "001.mp3"), album="Folge 39: Fall 77: Die verschleppte Schleppe",
                                     artist="Die Fuchsbande", title="Fall 77")
    card = SDCard(str(tmp_path / "sd"))
    card.scan()
    return card


def png_bytes(size=(300, 300), color=(0, 120, 255)):
    buffer = io.BytesIO()
    Image.new("RGB", size, color).save(buffer, "PNG")
    return buffer.getvalue()


def wait_workers(app, dialog):
    for _ in range(3):
        for worker in (dialog._research_worker, dialog._search_worker, dialog._series_detect_worker,
                       dialog._bible_worker, dialog._sheet_worker):
            if worker is not None:
                worker.wait(5000)
        app.processEvents()


@pytest.fixture
def no_network(monkeypatch):
    from core import cover_sources
    monkeypatch.setattr(cover_sources, "SEARCHERS", {})
    QSettings().setValue("covers/search_sources", [])


def test_series_tab_detects_assigns_and_anchors(app, sd, tmp_path, monkeypatch, no_network):
    from gui import cover_manager_dialog as cm

    monkeypatch.setattr(cm, "gemini_client", lambda: None)
    archive = CoverArchive(str(tmp_path / "data"))
    folder = sd.folders[1]
    dialog = cm.CoverManagerDialog(folder, "X", MetadataManager(), archive)
    dialog._start_research()
    wait_workers(app, dialog)

    # erkannt aus den Tags, noch kein Profil -> Eintrag "Erkannt: … (neu anlegen)"
    assert dialog._detected_series == ("Die Fuchsbande", "")
    assert dialog.series_profile is None
    labels = [dialog.series_combo.itemText(i) for i in range(dialog.series_combo.count())]
    assert labels == ["keine", "Erkannt: Die Fuchsbande (neu anlegen)"]
    assert "Keine Serie zugeordnet" in dialog.series_hint_label.text()

    # Auswahl des erkannten Eintrags legt das Profil an und ordnet das Album zu
    dialog.series_combo.setCurrentIndex(1)
    profile = dialog.series_profile
    assert profile is not None and profile.name == "Die Fuchsbande"
    assert archive.series.load(profile.id).name == "Die Fuchsbande"
    assert dialog.series_combo.currentData() == profile.id
    assert "0 Anker-Bild(er)" in dialog.series_hint_label.text()

    # Anker aus der Vorschau: neue KI-Variante anklicken, als Anker -> im Archiv behalten + Rolle + Profil
    dialog._generation_meta = {"style": "Aquarell", "model": "m", "prompt": ""}
    dialog._on_variant_ready(Image.new("RGB", (400, 600), "green"))
    dialog._add_anchor_from_preview()
    profile = dialog.series_profile
    assert [(a.side) for a in profile.anchors] == [SIDE_FRONT]
    covers = archive.covers(dialog.album_id)
    assert len(covers) == 1 and ROLE_SERIES_ANCHOR in covers[0].roles
    assert profile.anchors[0].cover_id == covers[0].id
    assert archive.load_album(dialog.album_id).series_id == profile.id
    assert dialog.anchor_list.count() == 1 and len(dialog._anchor_images) == 1
    assert "Serie: Die Fuchsbande – 1 Anker-Bild(er) werden als Vorlage mitgegeben" in dialog.series_hint_label.text()
    assert "⚓" in dialog._item_text(dialog.selected)

    # Stilbibel wird (verzoegert) gespeichert und geht in die Anfrage
    dialog.bible_edit.setPlainText("Figuren: Fuchs Finn")
    dialog._series_save_timer.stop()
    dialog._save_series_profile()
    assert archive.series.load(profile.id).style_bible == "Figuren: Fuchs Finn"
    fields = dialog._series_request_fields()
    assert fields["series_name"] == "Die Fuchsbande" and fields["series_bible"] == "Figuren: Fuchs Finn"
    assert fields["anchor_images"] == [covers[0].id and archive.load_bytes(dialog.album_id, covers[0])]

    # Anker entfernen
    dialog.anchor_list.setCurrentRow(0)
    dialog._remove_selected_anchor()
    assert dialog.series_profile.anchors == [] and dialog.anchor_list.count() == 0
    assert ROLE_SERIES_ANCHOR not in archive.covers(dialog.album_id)[0].roles

    # "keine" hebt die Zuordnung auf
    dialog.series_combo.setCurrentIndex(0)
    assert dialog.series_profile is None
    assert archive.load_album(dialog.album_id).series_id == ""
    assert dialog._series_request_fields() == {}
    monkeypatch.setattr(dialog, "unsaved_variants", lambda: [])
    dialog.done(0)

    # zweites Oeffnen: vorhandenes Profil steht zur Wahl, erkannte Reihe passt dazu -> automatisch zugeordnet
    dialog2 = cm.CoverManagerDialog(folder, "X", MetadataManager(), archive)
    dialog2._start_research()
    wait_workers(app, dialog2)
    assert dialog2.series_profile is not None and dialog2.series_profile.id == profile.id
    assert archive.load_album(dialog2.album_id).series_id == profile.id
    dialog2.done(0)


def test_series_tab_style_bible_and_character_sheet(app, sd, tmp_path, monkeypatch, no_network):
    from gui import cover_manager_dialog as cm

    calls = []

    class FakeInteractions:
        def create(self, **kwargs):
            calls.append(kwargs)
            if "response_format" in kwargs:
                import base64
                return SimpleNamespace(output_image=SimpleNamespace(
                    data=base64.b64encode(png_bytes((300, 200), (9, 9, 9))).decode()), output_text="")
            return SimpleNamespace(output_text="Figuren: Fuchs Finn.\nStil: Kinderbuch.")

    monkeypatch.setattr(cm, "gemini_client", lambda: SimpleNamespace(interactions=FakeInteractions()))
    monkeypatch.setattr(cm, "confirm_action", lambda *a, **k: True)
    archive = CoverArchive(str(tmp_path / "data"))
    profile = archive.series.create("Die Fuchsbande")
    archive.ensure_album("album-x", "Folge 39")
    archive.assign_album("album-x", profile.id)
    folder = sd.folders[1]
    dialog = cm.CoverManagerDialog(folder, "X", MetadataManager(), archive)
    monkeypatch.setattr(dialog, "_start_research", lambda: None)
    dialog.album_id = "album-x"
    dialog._load_series()
    assert dialog.series_profile.id == profile.id

    dialog.research_edit.setPlainText("Inhalt: Detektive im Wald")
    dialog._suggest_style_bible()
    wait_workers(app, dialog)
    assert dialog.bible_edit.toPlainText().startswith("Figuren: Fuchs Finn.")
    assert archive.series.load(profile.id).style_bible.startswith("Figuren: Fuchs Finn.")
    bible_call = next(c for c in calls if isinstance(c["input"], str) and "Stilbibel" in c["input"])
    assert "Detektive im Wald" in bible_call["input"]  # ohne Anker nur Text, ohne Google-Suche
    assert "tools" not in bible_call

    dialog._generate_character_sheet()
    wait_workers(app, dialog)
    sheet_call = next(c for c in calls if "response_format" in c)
    assert sheet_call["response_format"]["aspect_ratio"] == "3:2"
    assert isinstance(sheet_call["input"], str) and "Figuren: Fuchs Finn." in sheet_call["input"]  # keine Anker
    loaded = archive.series.load(profile.id)
    assert len(loaded.anchors) == 1 and loaded.anchors[0].album_id == "album-x"
    covers = archive.covers("album-x")
    assert len(covers) == 1 and covers[0].style == "Charakterblatt" and ROLE_SERIES_ANCHOR in covers[0].roles
    assert dialog.anchor_list.count() == 1
    assert any(c.entry is not None and c.entry.id == covers[0].id for c in dialog.candidates)
    dialog.done(0)


def test_series_style_takes_precedence_in_dialog(app, sd, tmp_path, monkeypatch, no_network):
    from gui import cover_manager_dialog as cm

    monkeypatch.setattr(cm, "gemini_client", lambda: object())
    archive = CoverArchive(str(tmp_path / "data"))
    profile = archive.series.create("Die Fuchsbande")
    profile.style = "Aquarell"
    archive.series.save(profile)
    archive.ensure_album("album-x", "Folge 39")
    archive.assign_album("album-x", profile.id)
    dialog = cm.CoverManagerDialog(sd.folders[1], "X", MetadataManager(), archive)
    monkeypatch.setattr(dialog, "_start_research", lambda: None)
    dialog.album_id = "album-x"
    dialog._load_series()
    dialog.style_combo.setCurrentText("Kinderbuch-Illustration")

    # Serien-Stil gewinnt gegen den Stil aus "KI erzeugen" und wird dort angezeigt
    assert dialog._effective_style() == "Aquarell"
    assert "Aquarell" in dialog.series_hint_label.text() and "Vorrang" in dialog.series_hint_label.text()

    requests = []

    class FakeSignal:
        def connect(self, slot):
            pass

    class FakeWorker:
        variant_ready = failed = completed = finished = FakeSignal()

        def __init__(self, source, request, client, model, count):
            requests.append(request)

        def start(self):
            pass

        def isRunning(self):
            return False

    monkeypatch.setattr(cm, "CoverGenerateWorker", lambda *a: FakeWorker(*a))
    dialog._start_generation()
    assert requests[-1].style == "Aquarell"

    # ohne festen Serien-Stil gilt wieder "KI erzeugen"
    cm._select_data(dialog.series_style_combo, "")
    assert dialog._effective_style() == "Kinderbuch-Illustration"
    dialog.done(0)
