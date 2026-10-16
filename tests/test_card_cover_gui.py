import io

import pytest

pytest.importorskip("PyQt6")

from PIL import Image
from PyQt6.QtCore import QCoreApplication, QSettings
from PyQt6.QtWidgets import QApplication

from core import card_store, cover_research
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
    (tmp_path / "01").mkdir()
    (tmp_path / "01" / "001.mp3").write_bytes(b"not really mp3")
    card = SDCard(str(tmp_path))
    card.scan()
    return card


def png_bytes():
    buffer = io.BytesIO()
    Image.new("RGB", (300, 300), (0, 120, 255)).save(buffer, "PNG")
    return buffer.getvalue()


def test_cover_dialog_blur_flow_saves_card(app, sd, monkeypatch):
    from gui import card_cover_dialog

    monkeypatch.setattr(card_cover_dialog, "gemini_client", lambda: None)
    monkeypatch.setattr(cover_research, "lookup_online_cover", lambda info: png_bytes())

    folder = sd.folders[1]
    dialog = card_cover_dialog.CardCoverDialog(folder, "Ordner 01", MetadataManager())
    dialog._start_research()
    dialog._research_worker.wait(5000)
    app.processEvents()

    assert dialog.source_combo.currentData() == "blur"
    assert dialog.brief.reference_source == "Cover Art Archive"

    dialog.title_edit.setText("Testtitel")
    dialog._start_generation()
    dialog._generate_worker.wait(5000)
    app.processEvents()
    assert dialog.current_art is not None

    dialog._save()
    assert card_store.has_card(folder.path)
    assert card_store.load_meta(folder.path).title == "Testtitel"
    assert folder.card_path


def test_print_sheet_dialog_renders_items(app, sd):
    from gui.print_sheet_dialog import PrintSheetDialog

    folder = sd.folders[1]
    card_store.save_card(folder.path, Image.new("RGB", (600, 900), "red"), card_store.CardMeta(title="X"), 1)
    folder.card_path = str(card_store.card_paths(folder.path)[0])

    dialog = PrintSheetDialog([folder], {1: "X"})
    dialog.copies_spin.setValue(3)
    assert len(dialog._items()) == 3
