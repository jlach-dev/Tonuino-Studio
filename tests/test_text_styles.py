import io

import pytest
from PIL import Image

from core import card_store
from core.card_layout import CardSpec, CardText, ORIENTATION_PORTRAIT, TextStyle, find_font_file, render_card


def test_text_style_roundtrip_and_color():
    style = TextStyle(family="Arial", size_mm=4.5, bold=False, color="#ffcc00")
    assert TextStyle.from_dict(style.to_dict()) == style
    assert style.rgba() == (255, 204, 0, 255)
    assert TextStyle(color="kaputt").rgba() == (255, 255, 255, 255)
    assert TextStyle.from_dict({"family": "X", "unbekannt": 1}).family == "X"


def test_fixed_size_and_color_are_rendered():
    art = Image.new("RGB", (600, 900), (0, 0, 0))
    yellow = TextStyle(size_mm=5.0, color="#ffff00")
    card = render_card(art, CardText("Heia", 1, "Reihe", title_style=yellow, series_style=TextStyle(size_mm=2.5)),
                       ORIENTATION_PORTRAIT)
    assert card.size == CardSpec().size_px(ORIENTATION_PORTRAIT)
    # irgendwo im unteren Drittel muss reines Gelb auftauchen
    pixels = card.crop((0, card.height * 2 // 3, card.width, card.height)).getcolors(maxcolors=200000)
    assert any(color == (255, 255, 0) for _count, color in pixels)


def test_find_font_file_unknown_family_is_none():
    assert find_font_file("Diese Schrift gibt es nicht") is None


def test_card_store_keeps_text_styles(tmp_path):
    meta = card_store.CardMeta(title="T", series="S", title_style={"color": "#ff0000", "size_mm": 3.0},
                               series_style={"family": "Arial"})
    card_store.save_card(str(tmp_path), Image.new("RGB", (400, 600)), meta, 2)
    loaded = card_store.load_meta(str(tmp_path))
    assert loaded.title_style["color"] == "#ff0000" and loaded.series_style["family"] == "Arial"
    assert card_store.render_from_meta(Image.new("RGB", (400, 600)), loaded, 2).size == CardSpec().size_px(
        ORIENTATION_PORTRAIT)


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


def test_preview_shows_card_only_on_card_tab_and_style_dialog(qt_app, tmp_path, monkeypatch):
    from core.metadata import MetadataManager
    from core.sd_card import SDCard
    from gui import cover_manager_dialog as cm
    from gui.text_style_dialog import TextStyleDialog

    folder = tmp_path / "sd" / "01"
    folder.mkdir(parents=True)
    (folder / "001.mp3").write_bytes(b"x")
    sd = SDCard(str(tmp_path / "sd"))
    sd.scan()
    monkeypatch.setattr(cm, "gemini_client", lambda: None)
    monkeypatch.setattr(cm.CoverManagerDialog, "_start_research", lambda self: None)
    dialog = cm.CoverManagerDialog(sd.folders[1], "X", MetadataManager(), None)
    dialog.resize(1200, 800)

    buffer = io.BytesIO()
    Image.new("RGB", (500, 500), (0, 0, 255)).save(buffer, "PNG")
    dialog._add_candidate(cm.Candidate(cm.SECTION_ONLINE, "Deezer", buffer.getvalue(), "online"), select=True)

    dialog.tabs.setCurrentIndex(0)  # Cover suchen: Bild selbst, quadratisch
    dialog._render_preview()
    assert not dialog._preview_shows_card
    square = dialog.preview.pixmap()
    assert abs(square.width() - square.height()) <= 2

    dialog.tabs.setCurrentIndex(1)  # Karte: fertige Karte im Kartenformat
    dialog._render_preview()
    assert dialog._preview_shows_card
    card = dialog.preview.pixmap()
    assert card.height() > card.width() * 1.3

    # Zahnrad-Dialog: Stil uebernehmen landet in Meta und Einstellungen
    style_dialog = TextStyleDialog("Titel", TextStyle(), dialog)
    style_dialog.size_spin.setValue(4.0)
    style_dialog._set_color("#00ff00")
    style = style_dialog.result_style()
    assert style.size_mm == 4.0 and style.color == "#00ff00" and style.family == ""
    monkeypatch.setattr(cm, "TextStyleDialog", lambda *a, **k: _Accepting(style_dialog))
    dialog._edit_text_style("title", "Titel")
    assert dialog.title_style.color == "#00ff00"
    assert dialog._card_meta_now(None).title_style["size_mm"] == 4.0
    monkeypatch.setattr(dialog, "unsaved_variants", lambda: [])
    dialog.done(0)


class _Accepting:
    def __init__(self, inner):
        self.inner = inner

    def exec(self):
        from PyQt6.QtWidgets import QDialog
        return QDialog.DialogCode.Accepted

    def result_style(self):
        return self.inner.result_style()
