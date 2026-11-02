"""Das Oeffnen des Cover-Managers kostet nichts: Die KI-Recherche laeuft nur auf Knopfdruck oder vor einem KI-Aufruf"""
import pytest

pytest.importorskip("PyQt6")

from PyQt6.QtCore import QCoreApplication
from PyQt6.QtWidgets import QApplication

from core.cover_archive import CoverArchive
from core.cover_research import CoverBrief, FolderInfo
from core.metadata import MetadataManager
from core.sd_card import Folder, Track
from gui import cover_manager_dialog as cm
from tests.test_library import make_album

KEY = object()


@pytest.fixture(scope="module")
def app():
    QCoreApplication.setOrganizationName("Tonuino-Manager-Tests")
    QCoreApplication.setApplicationName("tests-research")
    return QApplication.instance() or QApplication([])


class Signal:
    def __init__(self):
        self.slots = []

    def connect(self, slot):
        self.slots.append(slot)

    def emit(self, *args):
        for slot in self.slots:
            slot(*args)


@pytest.fixture
def dialog(app, tmp_path, monkeypatch):
    """Dialog mit Key; Worker sind Attrappen, calls haelt die Reihenfolge fest"""
    calls = []

    class FakeResearch:
        fail = False

        def __init__(self, folder, manager, client, model, extra, online_cover=True):
            self.client = client
            self.completed, self.failed, self.finished = Signal(), Signal(), Signal()

        def start(self):
            calls.append("ki" if self.client is KEY else "tags")
            if self.client is KEY and FakeResearch.fail:
                self.failed.emit("kaputt")
            else:
                self.completed.emit(FolderInfo(album="A"), CoverBrief(summary="Text" if self.client else ""))
            self.finished.emit()

        def isRunning(self):
            return False

        def wait(self, *args):
            return True

    class FakeGenerate:
        def __init__(self, *args):
            self.variant_ready, self.failed, self.completed, self.finished = Signal(), Signal(), Signal(), Signal()

        def start(self):
            calls.append("bild")

        def isRunning(self):
            return False

        def wait(self, *args):
            return True

    monkeypatch.setattr(cm, "gemini_client", lambda: KEY)
    monkeypatch.setattr(cm, "CoverResearchWorker", FakeResearch)
    monkeypatch.setattr(cm, "CoverGenerateWorker", FakeGenerate)
    monkeypatch.setattr(cm.CoverManagerDialog, "_start_search", lambda self: None)
    monkeypatch.setattr(cm.CoverManagerDialog, "_start_series_detection", lambda self: None)
    monkeypatch.setattr(cm.CoverManagerDialog, "unsaved_variants", lambda self: [])

    album = make_album(tmp_path / "Musik", "A", count=1, album="A", artist="B")
    folder = Folder(index=None, path=str(album), name="A",
                    tracks=[Track(index=1, filename="01.mp3", filepath=str(album / "01.mp3"))])
    archive = CoverArchive(str(tmp_path / "data"))
    instance = cm.CoverManagerDialog(folder, "A", MetadataManager(), archive, library=True, album_id="a")
    app.processEvents()
    instance.calls = calls
    instance.fake_research = FakeResearch
    yield instance
    instance.done(0)


def test_opening_only_reads_tags(dialog):
    assert dialog.calls == ["tags"]
    assert dialog.research_edit.toPlainText() == ""


def test_button_starts_ai_research(dialog):
    dialog.btn_research.click()
    assert dialog.calls == ["tags", "ki"]
    assert dialog.research_edit.toPlainText().endswith("Text")


def test_generation_researches_first_and_only_once(dialog):
    dialog._start_generation()
    assert dialog.calls == ["tags", "ki", "bild"]
    dialog._start_generation()
    assert dialog.calls == ["tags", "ki", "bild", "bild"]


def test_own_text_replaces_research(dialog):
    dialog.research_edit.document().setModified(True)
    dialog._start_generation()
    assert dialog.calls == ["tags", "bild"]


def test_failed_research_does_not_block_generation(dialog):
    dialog.fake_research.fail = True
    dialog._start_generation()
    assert dialog.calls == ["tags", "ki", "bild"]


def test_closing_drops_waiting_ai_call(dialog):
    dialog._after_research = dialog._start_generation
    dialog.done(0)
    assert dialog._after_research is None
    dialog._run_after_research()
    assert "bild" not in dialog.calls
