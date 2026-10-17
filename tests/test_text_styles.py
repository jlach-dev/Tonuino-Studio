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
    from gui.text_style_dialog import TextStylesPopup

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

    # Zahnrad: ein Popup fuer beide Bereiche, Aenderungen gehen live in die Vorschau
    renders = []
    monkeypatch.setattr(dialog, "_render_preview", lambda *a: renders.append(True))
    dialog._open_text_styles()
    popup = dialog._styles_popup
    assert isinstance(popup, TextStylesPopup) and popup.isVisible()
    popup.title_editor.size_spin.setValue(4.0)
    popup.title_editor._set_color("#00ff00")
    popup.title_editor.changed.emit()
    popup.series_editor.bold_check.setChecked(False)
    assert dialog.title_style.size_mm == 4.0 and dialog.title_style.color == "#00ff00"
    assert dialog.series_style.bold is False
    assert len(renders) >= 2
    assert dialog._card_meta_now(None).title_style["size_mm"] == 4.0

    # Abbrechen stellt die Ausgangswerte wieder her, Fertig speichert sie als Vorgabe
    popup.reject()
    assert dialog.title_style.size_mm == 0.0 and dialog.series_style.bold is True
    dialog._open_text_styles()
    popup = dialog._styles_popup
    popup.title_editor.size_spin.setValue(3.0)
    popup.accept()
    from PyQt6.QtCore import QSettings
    assert '"size_mm": 3.0' in QSettings().value(cm.SETTINGS_TITLE_STYLE, "", type=str)
    monkeypatch.setattr(dialog, "unsaved_variants", lambda: [])
    dialog.done(0)


def test_cover_manager_album_focus_opens_search_tab(qt_app, tmp_path, monkeypatch):
    from core.metadata import MetadataManager
    from core.sd_card import SDCard
    from gui import cover_manager_dialog as cm

    folder = tmp_path / "sd" / "01"
    folder.mkdir(parents=True)
    (folder / "001.mp3").write_bytes(b"x")
    sd = SDCard(str(tmp_path / "sd"))
    sd.scan()
    monkeypatch.setattr(cm, "gemini_client", lambda: None)
    monkeypatch.setattr(cm.CoverManagerDialog, "_start_research", lambda self: None)
    dialog = cm.CoverManagerDialog(sd.folders[1], "X", MetadataManager(), None, focus=cm.CoverManagerDialog.FOCUS_ALBUM)
    assert dialog.tabs.currentIndex() == 0
    assert "Album-Cover ändern" in dialog.status_label.text()
    assert "Album-Cover" in dialog.windowTitle() and "Kartencover" in dialog.windowTitle()
    dialog.done(0)
