import base64
import io
from types import SimpleNamespace

import pytest
from PIL import Image

from core import card_store
from core.card_layout import CardSpec, ORIENTATION_LANDSCAPE, ORIENTATION_PORTRAIT, fits_card_format
from core.cover_archive import KIND_ONLINE, CoverArchive
from core.cover_generator import EXTEND_SEAM_INSET, ExtendMasking, ImageRefused, extend_masked, insert_original
from core.image_masking import EFFECT_BLUR, EFFECT_FILL, MaskShape, SHAPE_ELLIPSE, SHAPE_RECT, apply_masks, shape_mask
from core.sd_card import SDCard


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


def png_bytes(size=(300, 300), color=(0, 120, 255)):
    buffer = io.BytesIO()
    Image.new("RGB", size, color).save(buffer, "PNG")
    return buffer.getvalue()


def checker(size=(200, 200)):
    image = Image.new("RGB", size, (255, 255, 255))
    for y in range(0, size[1], 4):
        for x in range(0, size[0], 4):
            if (x + y) // 4 % 2:
                image.paste((0, 0, 0), (x, y, x + 4, y + 4))
    return image


# --- Abdecken ------------------------------------------------------------------------------------

def test_shape_mask_covers_rectangle_and_ellipse():
    rect = shape_mask((100, 100), [MaskShape(SHAPE_RECT, 0.1, 0.1, 0.2, 0.2)])
    assert rect.getpixel((10, 10)) == 255 and rect.getpixel((29, 29)) == 255 and rect.getpixel((30, 30)) == 0
    ellipse = shape_mask((100, 100), [MaskShape(SHAPE_ELLIPSE, 0.0, 0.0, 1.0, 1.0)])
    assert ellipse.getpixel((50, 50)) == 255 and ellipse.getpixel((1, 1)) == 0


def test_apply_masks_only_changes_covered_area_and_keeps_original():
    image = checker()
    shape = MaskShape(SHAPE_RECT, 0.25, 0.25, 0.5, 0.5)
    for effect in (EFFECT_BLUR, EFFECT_FILL):
        result = apply_masks(image, [shape], effect)
        assert result.getpixel((10, 10)) == image.getpixel((10, 10))
        inner = result.crop((60, 60, 140, 140)).convert("L")
        low, high = inner.getextrema()
        assert high - low < 40  # das Schachbrett ist nicht mehr zu erkennen
    assert image.getpixel((100, 100)) in ((0, 0, 0), (255, 255, 255))


def test_fill_uses_color_of_each_shape():
    image = Image.new("RGB", (100, 100), (255, 0, 0))
    image.paste((0, 0, 255), (50, 0, 100, 100))
    result = apply_masks(image, [MaskShape(SHAPE_RECT, 0, 0, 0.4, 1), MaskShape(SHAPE_RECT, 0.6, 0, 0.4, 1)], EFFECT_FILL)
    assert result.getpixel((10, 50)) == (255, 0, 0) and result.getpixel((90, 50)) == (0, 0, 255)


def test_shape_at_the_very_edge_stays_inside_the_image():
    assert MaskShape(SHAPE_RECT, 0.99, 0.99, 0.5, 0.5).box((100, 100)) == (99, 99, 100, 100)


# --- Original wieder einsetzen -------------------------------------------------------------------

@pytest.mark.parametrize("size, orientation, canvas", [
    ((400, 400), ORIENTATION_PORTRAIT, (400, 600)),  # oben und unten erweitert
    ((400, 400), ORIENTATION_LANDSCAPE, (600, 400)),  # links und rechts erweitert
])
def test_hard_seam_is_inset_only_on_extended_sides(size, orientation, canvas):
    original = Image.new("RGB", size, (255, 0, 0))
    ai_image = Image.new("RGB", (canvas[0] * 2, canvas[1] * 2), (0, 255, 0))
    result = insert_original(ai_image, original, ())
    assert result.size == canvas
    band = round(400 * EXTEND_SEAM_INSET)
    assert band == 10
    if canvas[1] > 400:
        assert result.getpixel((200, 100)) == (0, 255, 0)  # erster Streifen des Bildes noch von der KI
        assert result.getpixel((200, 100 + band - 1)) == (0, 255, 0)
        assert result.getpixel((200, 100 + band)) == (255, 0, 0)  # harte Kante: sofort das Original
        assert result.getpixel((200, 500 - band - 1)) == (255, 0, 0)
        assert result.getpixel((200, 500 - band)) == (0, 255, 0)
        assert result.getpixel((0, 300)) == (255, 0, 0)  # nicht erweiterte Seite: bis ganz an den Rand
    else:
        assert result.getpixel((100 + band - 1, 200)) == (0, 255, 0)
        assert result.getpixel((100 + band, 200)) == (255, 0, 0)
        assert result.getpixel((300, 0)) == (255, 0, 0)


def test_generated_image_is_not_distorted_to_exact_three_two():
    """Die KI liefert 2528 x 1696, nicht 3:2. Gleichmaessig skalieren, sonst springen Linien an der Naht."""
    original = Image.new("RGB", (640, 640), (0, 0, 0))
    ai_image = Image.new("RGB", (2528, 1696), (0, 0, 0))
    ai_scale = 1696 / 640
    ai_left = (2528 - 1696) // 2
    for x in (17, 620):  # senkrechte Linien im Original und an derselben Stelle im KI-Bild
        original.paste((255, 255, 255), (x, 0, x + 3, 640))
        ai_image.paste((255, 255, 255), (ai_left + round(x * ai_scale), 0, ai_left + round((x + 3) * ai_scale), 1696))
    result = insert_original(ai_image, original, ())
    assert result.size == (954, 640)
    x0 = (954 - 640) // 2
    for x in (17, 620):  # liegt im KI-Streifen der Naht und bleibt genau dort
        assert result.getpixel((x0 + x + 1, 320))[0] > 200
        assert result.getpixel((x0 + x - 3, 320))[0] < 60


def test_covered_area_comes_from_the_original_up_to_the_edge():
    original = Image.new("RGB", (400, 400), (255, 0, 0))
    ai_image = Image.new("RGB", (400, 600), (0, 255, 0))
    logo = MaskShape(SHAPE_RECT, 0.25, 0.0, 0.5, 0.2)  # Schriftzug am oberen Rand
    result = insert_original(ai_image, original, (logo,))
    assert result.getpixel((200, 100)) == (255, 0, 0)  # im Streifen, aber abgedeckt
    assert result.getpixel((50, 100)) == (0, 255, 0)  # im Streifen, nicht abgedeckt


def test_soft_seam_blends():
    original = Image.new("RGB", (400, 400), (255, 0, 0))
    ai_image = Image.new("RGB", (400, 600), (0, 255, 0))
    result = insert_original(ai_image, original, (), soft=True)
    red, green, _ = result.getpixel((200, 105))
    assert 0 < red < 255 and 0 < green < 255
    assert result.getpixel((200, 100)) == (0, 255, 0) and result.getpixel((200, 110)) == (255, 0, 0)


def test_extend_masked_sends_only_the_covered_image():
    original = checker((400, 400))
    buffer = io.BytesIO()
    original.save(buffer, "PNG")
    calls = []

    def create(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(output_image=SimpleNamespace(data=png_bytes((400, 600), (0, 255, 0))), output_text="")

    client = SimpleNamespace(interactions=SimpleNamespace(create=create))
    masking = ExtendMasking((MaskShape(SHAPE_RECT, 0.25, 0.25, 0.5, 0.5),), EFFECT_FILL)
    result = extend_masked(client, "m", buffer.getvalue(), ORIENTATION_PORTRAIT, masking)

    sent = Image.open(io.BytesIO(base64.b64decode(calls[0]["input"][1]["data"]))).convert("RGB")
    assert sent.size == (400, 400)
    assert sent.crop((120, 120, 280, 280)).getcolors(maxcolors=4) is not None  # Flaeche statt Schachbrett
    assert sent.crop((120, 120, 280, 280)) != original.crop((120, 120, 280, 280))
    assert "placeholders" in calls[0]["input"][0]["text"]
    # Das Ergebnis traegt in der Mitte wieder das Original
    assert result.crop((120, 220, 280, 380)).tobytes() == original.crop((120, 120, 280, 280)).tobytes()


def test_worker_reports_refusal_separately(qt_app):
    from gui.workers import CoverExtendWorker

    def create(**kwargs):
        raise RuntimeError("Error code: 400 - {'error': {'code': 'content_blocked'}}")

    client = SimpleNamespace(interactions=SimpleNamespace(create=create))
    worker = CoverExtendWorker(client, "m", png_bytes(), ORIENTATION_PORTRAIT)
    refused, failed = [], []
    worker.refused.connect(refused.append)
    worker.failed.connect(failed.append)
    worker.run()
    assert refused and not failed and "abgelehnt" in refused[0]

    def broken(**kwargs):
        raise RuntimeError("Netz weg")

    worker = CoverExtendWorker(SimpleNamespace(interactions=SimpleNamespace(create=broken)), "m", png_bytes(),
                               ORIENTATION_PORTRAIT)
    refused, failed = [], []
    worker.refused.connect(refused.append)
    worker.failed.connect(failed.append)
    worker.run()
    assert failed and not refused


# --- Format ---------------------------------------------------------------------------------------

def test_format_check_tolerates_small_differences_only():
    assert fits_card_format(Image.new("RGB", (900, 600)), ORIENTATION_LANDSCAPE)
    assert not fits_card_format(Image.new("RGB", (800, 600)), ORIENTATION_LANDSCAPE)
    assert not fits_card_format(Image.new("RGB", (500, 500)), ORIENTATION_LANDSCAPE)


def test_transfer_keeps_saved_art_with_old_blur_edge(tmp_path):
    """Eine Karte mit eingerechnetem Blur-Rand behaelt ihn beim Uebertragen, statt neu zugeschnitten zu werden"""
    from core.library_import import _card_art

    archive = CoverArchive(str(tmp_path / "data"))
    entry = archive.add_cover("album", Image.new("RGB", (500, 500), (0, 0, 200)), "online")
    folder = SimpleNamespace(path=str(tmp_path / "01"))
    (tmp_path / "01").mkdir()
    assert _card_art(folder, "album", entry, archive, None).size == (500, 500)  # noch nichts gestaltet

    designed = card_store.CardMeta(title="X", archive_cover_id=entry.id)
    archive.card_dir("album").mkdir(parents=True, exist_ok=True)
    card_store.save_card(str(archive.card_dir("album")), Image.new("RGB", (700, 1000), (1, 2, 3)), designed, 1)
    assert _card_art(folder, "album", entry, archive, designed).size == (700, 1000)
    other = card_store.CardMeta(title="X", archive_cover_id="anderes")
    assert _card_art(folder, "album", entry, archive, other).size == (500, 500)  # gehoert zu einem anderen Cover


# --- Dialoge -------------------------------------------------------------------------------------

@pytest.fixture
def manager(qt_app, tmp_path, monkeypatch):
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
    monkeypatch.setattr(cm.CoverManagerDialog, "unsaved_variants", lambda self: [])
    dialog = cm.CoverManagerDialog(sd.folders[1], "Eule", MetadataManager(), CoverArchive(str(tmp_path / "data")))
    yield cm, dialog
    dialog.done(0)


def test_gallery_has_no_current_tab_and_slots_show_what_is_set(manager):
    cm, dialog = manager
    labels = [dialog.filter_bar.tabText(i) for i in range(dialog.filter_bar.count())]
    assert not any("Aktuell" in label for label in labels)
    picture = cm.Candidate(cm.SECTION_ONLINE, "Deezer", png_bytes(color=(0, 0, 200)), KIND_ONLINE)
    dialog._add_candidate(picture, select=True)
    assert dialog.slot_front._pixmap is None
    assert dialog.btn_album_cover.parent() is not None

    dialog.btn_card_cover.click()
    assert dialog.slot_front._pixmap is not None  # die fertige Karte
    assert dialog.slot_front.caption_text == "Deezer (Vorderseite)"
    assert dialog._set_roles(picture) == ["vorne"]
    current = dialog._current_candidate("is_sd_card_art")
    item = next(i for i in dialog._items() if i.data(cm.Qt.ItemDataRole.UserRole) is current)
    assert item.isHidden()  # Gesetztes liegt nicht in der Galerie
    assert dialog.filter_bar.tabText(0) == "Alle (1)"

    # Klick auf den Platz zeigt das gesetzte Bild groß
    dialog._show_current("is_sd_card_art")
    assert dialog.selected is current


def test_blur_button_creates_new_version_without_assigning(manager):
    cm, dialog = manager
    picture = cm.Candidate(cm.SECTION_ONLINE, "Deezer", png_bytes(), KIND_ONLINE)
    dialog._add_candidate(picture, select=True)
    dialog.btn_blur.click()
    new = dialog.selected
    assert new.section == cm.SECTION_NEW and new.label.startswith("Blur-Rand · Deezer")
    assert new.image().size == CardSpec().size_px(dialog.orientation_combo.currentData())
    assert not card_store.has_card(dialog._card_dir) and dialog.front_source is None
    dialog.btn_blur.click()  # die neue Version passt schon: keine weitere
    assert sum(c.section == cm.SECTION_NEW for c in dialog.candidates) == 1
    dialog.btn_card_cover.click()
    assert card_store.has_card(dialog._card_dir) and new.kept


def test_info_field_names_selection_and_sides(manager):
    cm, dialog = manager
    picture = cm.Candidate(cm.SECTION_ONLINE, "Deezer", png_bytes(), KIND_ONLINE)
    dialog._add_candidate(picture, select=True)
    assert "300 × 300" in dialog.selection_label.text()
    dialog.btn_card_cover.click()
    assert "Vorne: Deezer (Vorderseite)" in dialog.sides_label.text()
    assert "Karte gespeichert ✓" in dialog.sides_label.text()


def test_slots_have_fixed_frames_and_show_no_image(manager):
    cm, dialog = manager
    assert dialog.slot_album.image.text() == "no image" and dialog.slot_back.image.text() == "no image"
    assert dialog.slot_album.frame_size == dialog.SLOT_ALBUM
    cm._select_data(dialog.orientation_combo, ORIENTATION_LANDSCAPE)
    assert dialog.slot_front.frame_size == dialog.SLOT_CARD_LANDSCAPE
    cm._select_data(dialog.orientation_combo, ORIENTATION_PORTRAIT)
    assert dialog.slot_back.frame_size == dialog.SLOT_CARD_PORTRAIT

    # Der Rahmen waechst mit dem Platz, behaelt sein Verhaeltnis und liegt im Feld
    slot = dialog.slot_front
    slot.area.resize(600, 300)
    dialog.show()
    slot.area.resize(600, 300)
    qt_app = cm.QApplication.instance()
    qt_app.processEvents()
    slot._place_frame()
    expected = dialog.SLOT_CARD_PORTRAIT[0] / dialog.SLOT_CARD_PORTRAIT[1]
    assert abs(slot.image.width() / slot.image.height() - expected) < 0.02
    assert slot.image.height() > dialog.SLOT_CARD_PORTRAIT[1]
    dialog.hide()
    width, height = CardSpec().size_px(ORIENTATION_LANDSCAPE)
    assert abs(dialog.SLOT_CARD_LANDSCAPE[0] / dialog.SLOT_CARD_LANDSCAPE[1] - width / height) < 0.02
    assert abs(dialog.SLOT_CARD_PORTRAIT[0] / dialog.SLOT_CARD_PORTRAIT[1] - height / width) < 0.02
    picture = cm.Candidate(cm.SECTION_ONLINE, "Deezer", png_bytes(), KIND_ONLINE)
    dialog._add_candidate(picture, select=True)
    dialog.btn_card_cover.click()
    assert dialog.slot_front.image.pixmap() is not None and not dialog.slot_front.image.pixmap().isNull()
    assert dialog.slot_back.image.text() == "no image"


def test_all_panels_have_visible_movable_dividers(manager):
    _, dialog = manager
    for splitter in (dialog.main_splitter, dialog.top_splitter, dialog.bottom_splitter):
        assert splitter.count() == 2 and splitter.handleWidth() == 8
        assert not splitter.handle(1).isHidden()


def test_back_tab_is_disabled_without_back_side(manager):
    cm, dialog = manager
    plain = cm.Candidate(cm.SECTION_ONLINE, "Deezer", png_bytes(), KIND_ONLINE)
    both = cm.Candidate(cm.SECTION_ONLINE, "MB", png_bytes(), KIND_ONLINE, back_data=png_bytes(color=(1, 2, 3)))
    dialog._add_candidate(plain, select=True)
    dialog._add_candidate(both)
    assert not dialog.side_bar.isTabEnabled(1)
    dialog.gallery.setCurrentRow(1)
    assert dialog.selected is both and dialog.side_bar.isTabEnabled(1)
    dialog.side_bar.setCurrentIndex(1)
    dialog.gallery.setCurrentRow(0)
    assert dialog.side_bar.currentIndex() == 0 and not dialog.side_bar.isTabEnabled(1)
    dialog.tabs.setCurrentIndex(1)  # im Reiter "Karte" zählt die Karte, nicht das Bild
    assert dialog.side_bar.isTabEnabled(1)
    assert dialog.preview_title.text() == "Karte bearbeiten"


def test_image_is_set_as_it_is(manager):
    cm, dialog = manager
    picture = cm.Candidate(cm.SECTION_ONLINE, "Deezer", png_bytes(), KIND_ONLINE)
    dialog._add_candidate(picture, select=True)
    dialog.btn_card_cover.click()
    assert Image.open(card_store.card_paths(dialog._card_dir)[1]).size == (300, 300)  # kein Rand eingerechnet
    size = CardSpec().size_px(dialog.orientation_combo.currentData())
    assert Image.open(card_store.card_paths(dialog._card_dir)[0]).size == size  # die Karte ist zugeschnitten


def test_refusal_offers_masking_and_keeps_shapes(manager, monkeypatch):
    cm, dialog = manager
    picture = cm.Candidate(cm.SECTION_ONLINE, "Deezer", png_bytes((400, 400)), KIND_ONLINE)
    dialog._add_candidate(picture, select=True)
    started = []

    class Signal:
        def connect(self, slot):
            pass

    class Worker:
        done = failed = refused = finished = Signal()

        def __init__(self, *args):
            started.append(args)

        def start(self):
            pass

        def isRunning(self):
            return False

    shape = MaskShape(SHAPE_RECT, 0.1, 0.1, 0.2, 0.2)
    seen = []

    class FakeMask:
        def __init__(self, image, shapes, parent):
            seen.append(list(shapes))

        def exec(self):
            return cm.QDialog.DialogCode.Accepted

        def shapes(self):
            return [shape]

        def effect(self):
            return EFFECT_FILL

        def soft(self):
            return True

    monkeypatch.setattr(cm, "CoverExtendWorker", Worker)
    monkeypatch.setattr(cm, "CoverMaskDialog", FakeMask)
    dialog._extend_selected()
    assert len(started[0]) == 4  # der erste Versuch geht ohne Abdeckung
    dialog._open_mask_dialog()
    masking = started[1][4]
    assert masking.shapes == (shape,) and masking.effect == EFFECT_FILL and masking.soft
    dialog._open_mask_dialog()  # zweite Ablehnung: Formen bleiben stehen
    assert seen == [[], [shape]]
    dialog._on_extended(Image.new("RGB", (400, 600)))
    assert dialog.selected.label.startswith("KI-Rand (abgedeckt)")
    dialog.selected = picture
    dialog._extend_selected()  # ein neuer Lauf beginnt wieder ohne Formen
    assert dialog._mask_shapes == []


def test_refused_error_type_is_kept():
    assert "Häufige Gründe" in str(ImageRefused("extend"))


def test_mask_dialog_draw_move_delete_undo(qt_app):
    from PyQt6.QtCore import QPointF, Qt
    from PyQt6.QtTest import QTest
    from gui.cover_mask_dialog import CoverMaskDialog

    dialog = CoverMaskDialog(Image.new("RGB", (600, 600), (10, 20, 30)), [])
    dialog.resize(800, 800)
    dialog.show()
    qt_app.processEvents()
    assert not dialog.btn_send.isEnabled()
    viewport = dialog.view.viewport()

    def point(x, y):
        return dialog.view.mapFromScene(QPointF(x, y))

    def drag(start, end):
        QTest.mousePress(viewport, Qt.MouseButton.LeftButton, pos=point(*start))
        QTest.mouseMove(viewport, point(*end))
        QTest.mouseRelease(viewport, Qt.MouseButton.LeftButton, pos=point(*end))

    drag((100, 100), (300, 200))
    shapes = dialog.shapes()
    assert len(shapes) == 1 and shapes[0].kind == SHAPE_RECT and dialog.btn_send.isEnabled()
    assert shapes[0].x == pytest.approx(100 / 600, abs=0.02) and shapes[0].width == pytest.approx(200 / 600, abs=0.02)

    drag((200, 150), (250, 150))  # verschieben
    assert dialog.shapes()[0].x == pytest.approx(150 / 600, abs=0.02)
    dialog.undo()
    assert dialog.shapes()[0].x == pytest.approx(100 / 600, abs=0.02)

    dialog.btn_ellipse.click()
    drag((400, 400), (550, 550))
    assert [s.kind for s in dialog.shapes()] == [SHAPE_RECT, SHAPE_ELLIPSE]

    QTest.mouseClick(viewport, Qt.MouseButton.LeftButton, pos=point(450, 450))  # nur ein Klick: keine neue Form
    assert len(dialog.shapes()) == 2
    dialog.delete_selected()
    assert [s.kind for s in dialog.shapes()] == [SHAPE_RECT]

    QTest.mouseClick(viewport, Qt.MouseButton.LeftButton, pos=point(50, 500))  # Klick ins Freie: keine Form
    assert len(dialog.shapes()) == 1

    dialog.btn_preview.click()  # Vorschau zeigt das abgedeckte Bild, Zeichnen ist aus
    assert dialog.previewing()
    drag((400, 400), (550, 550))
    assert len(dialog.shapes()) == 1
    dialog.btn_preview.click()
    dialog.clear_shapes()
    assert not dialog.shapes() and not dialog.btn_send.isEnabled()
    dialog.undo()
    assert len(dialog.shapes()) == 1
    dialog.close()
