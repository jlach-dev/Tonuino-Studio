import base64
import io
from types import SimpleNamespace

import pytest
from PIL import Image, ImageChops, ImageDraw, ImageOps, ImageStat

from core import card_store
from core.card_layout import CardSpec, ORIENTATION_LANDSCAPE, ORIENTATION_PORTRAIT, fits_card_format
from core.cover_archive import KIND_ONLINE, CoverArchive
from core.cover_generator import (EXTEND_DOUBTFUL_DIFFERENCE, EXTEND_PROMPT, EXTEND_SEAM_LIMIT, ExtendMasking,
                                  ImageRefused, MaskedExtension, extend_masked, locate_original, restore_covered,
                                  seam_strength)
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


# --- Abgedeckte Stellen zurueckholen -------------------------------------------------------------

def motif(size=(400, 400)):
    """Ein Bild mit Struktur, an dem sich die Lage messen laesst"""
    image = Image.new("RGB", size, (40, 60, 90))
    draw = ImageDraw.Draw(image)
    for index, x in enumerate(range(0, size[0], 37)):
        draw.rectangle((x, 0, x + 11, size[1]), fill=(200 - index * 7, 120, 40 + index * 9))
    for index, y in enumerate(range(0, size[1], 53)):
        draw.rectangle((0, y, size[0], y + 9), fill=(30, 180 - index * 11, 200))
    return image


def generated_from(image, canvas=(600, 400), scale_x=1.0, scale_y=1.0, shift=(0, 0)):
    """So liefert die KI: das Motiv etwas anders skaliert und verschoben auf einer groesseren Flaeche"""
    width, height = round(image.width * canvas[1] / image.height * scale_x), round(canvas[1] * scale_y)
    result = Image.new("RGB", canvas, (90, 90, 90))
    left, top = (canvas[0] - width) // 2 + shift[0], (canvas[1] - height) // 2 + shift[1]
    scaled = image.resize((width, height), Image.Resampling.BILINEAR)
    result.paste(scaled, (left, top))
    # Wie bei einer gelungenen Erweiterung laeuft das Bild ohne Kante weiter: Randspalten fortsetzen
    if left > 0:
        result.paste(scaled.crop((0, 0, 1, height)).resize((left, height)), (0, top))
    if left + width < canvas[0]:
        result.paste(scaled.crop((width - 1, 0, width, height)).resize((canvas[0] - left - width, height)),
                     (left + width, top))
    return result


def boxed(image, canvas=(600, 400)):
    """Fehlerbild: das Motiv als Kasten auf einer fremden Flaeche, mit harter Kante"""
    result = Image.new("RGB", canvas, (150, 110, 70))
    width = round(image.width * canvas[1] / image.height)
    result.paste(image.resize((width, canvas[1])), ((canvas[0] - width) // 2, 0))
    return result


@pytest.mark.parametrize("scale_x, shift", [(1.0, (0, 0)), (0.96, (0, 0)), (1.03, (0, 0)), (0.97, (9, 0))])
def test_locate_finds_the_motif_when_the_ai_scales_it(scale_x, shift):
    original = motif()
    weight = Image.new("L", original.size, 255)
    generated = generated_from(original, scale_x=scale_x, shift=shift)
    left, top, right, bottom = locate_original(generated.convert("L"), original.convert("L"), weight)
    assert right - left == pytest.approx(400 * scale_x, abs=3)
    assert (left + right) / 2 == pytest.approx(300 + shift[0], abs=3)
    assert bottom - top == pytest.approx(400, abs=3)


def test_locate_ignores_what_is_covered():
    original = motif()
    shape = MaskShape(SHAPE_RECT, 0.2, 0.2, 0.3, 0.3)
    generated = generated_from(apply_masks(original, [shape], EFFECT_FILL), scale_x=0.96)
    weight = ImageOps.invert(shape_mask(original.size, [shape]))
    left, _, right, _ = locate_original(generated.convert("L"), original.convert("L"), weight)
    assert right - left == pytest.approx(384, abs=3)


def test_locate_stays_near_the_expected_place():
    """Dunkles Bild auf hellem Rand: Ausserhalb des KI-Bildes ist alles schwarz, das darf nicht locken"""
    original = Image.new("RGB", (400, 400), (0, 0, 0))
    generated = Image.new("RGB", (600, 400), (255, 255, 255))
    left, top, right, bottom = locate_original(generated.convert("L"), original.convert("L"),
                                               Image.new("L", original.size, 255))
    assert abs(left - 100) <= 40 and abs(right - 500) <= 40 and abs(top) <= 40 and abs(bottom - 400) <= 40


def test_only_covered_area_comes_from_the_original():
    original = Image.new("RGB", (400, 400), (255, 0, 0))
    generated = Image.new("RGB", (600, 400), (0, 255, 0))
    shape = MaskShape(SHAPE_RECT, 0.25, 0.25, 0.5, 0.5)
    result = restore_covered(generated, original, (shape,)).image
    assert result.size == (600, 400)
    assert result.getpixel((300, 200)) == (255, 0, 0)  # in der Form
    assert result.getpixel((150, 200)) == (0, 255, 0)  # im Motiv, aber nicht abgedeckt: bleibt von der KI
    assert result.getpixel((50, 200)) == (0, 255, 0)  # im neuen Rand


def test_patch_edge_is_soft_only_outside_the_shape():
    original = Image.new("RGB", (400, 400), (255, 0, 0))
    generated = Image.new("RGB", (600, 400), (0, 255, 0))
    result = restore_covered(generated, original, (MaskShape(SHAPE_RECT, 0.25, 0.25, 0.5, 0.5),)).image
    assert result.getpixel((205, 200)) == (255, 0, 0)  # gleich innerhalb der Form schon voll
    red, green, _ = result.getpixel((197, 200))  # knapp ausserhalb: Mischung
    assert red > 0 and green > 0


def test_patch_lands_on_the_motif_when_the_ai_scaled_it():
    """Das Rot liegt im KI-Bild schmaler und versetzt. Die Stelle muss trotzdem deckungsgleich sitzen."""
    original = motif()
    shape = MaskShape(SHAPE_RECT, 0.4, 0.4, 0.2, 0.2)
    generated = generated_from(original, scale_x=0.96, shift=(6, 0))
    result = restore_covered(generated, original, (shape,)).image
    # Das KI-Bild wird passend gestreckt: Ausserhalb der Form stimmt es jetzt mit dem Original ueberein
    x0 = round((generated.width - 384) / 2 + 6) / 0.96
    motif_area = result.crop((round(x0), 0, round(x0) + 400, 400)).convert("L")
    difference = ImageStat.Stat(ImageChops.difference(motif_area, original.convert("L"))).mean[0]
    assert difference < 12
    area = shape.box(original.size)
    inner = (area[0] + 10, area[1] + 10, area[2] - 10, area[3] - 10)
    placed = (inner[0] + round(x0), inner[1], inner[2] + round(x0), inner[3])
    close = ImageChops.difference(result.crop(placed).convert("L"), original.crop(inner).convert("L"))
    assert ImageStat.Stat(close).mean[0] < 6


def test_doubtful_when_the_edges_do_not_match():
    original = motif()
    good = restore_covered(generated_from(original, scale_x=0.97), original, ())
    assert not good.doubtful and good.edge_difference < 6
    bad = restore_covered(generated_from(ImageOps.mirror(original)), original, ())
    assert bad.doubtful


def test_seam_is_found_where_the_ai_pasted_a_box():
    original = motif()
    assert seam_strength(generated_from(original), original.size) < EXTEND_SEAM_LIMIT
    assert seam_strength(boxed(original), original.size) > 0.8
    # auch wenn die Kante ein paar Pixel neben der erwarteten Stelle liegt
    shifted = Image.new("RGB", (600, 400), (150, 110, 70))
    shifted.paste(original, (104, 0))
    assert seam_strength(shifted, original.size) > 0.8
    portrait = Image.new("RGB", (400, 600), (150, 110, 70))
    portrait.paste(original, (0, 100))
    assert seam_strength(portrait, original.size) > 0.8
    assert seam_strength(Image.new("RGB", (600, 400)), (600, 400)) == 0.0  # nichts erweitert


def test_masked_extension_with_box_is_doubtful():
    original = motif()
    result = restore_covered(boxed(original), original, ())
    assert result.seam > EXTEND_SEAM_LIMIT and result.doubtful


def test_prompt_forbids_a_pasted_panel():
    assert "seam" in EXTEND_PROMPT and "panel" in EXTEND_PROMPT


def test_worker_checks_plain_extension_for_a_seam(qt_app, monkeypatch):
    from core import cover_generator
    from gui.workers import CoverExtendWorker

    buffer = io.BytesIO()
    motif().save(buffer, "PNG")
    for generated, expected in ((boxed(motif()), ["doubtful"]), (generated_from(motif()), [])):
        monkeypatch.setattr(cover_generator, "extend_to_card", lambda *args, image=generated: image)
        worker = CoverExtendWorker(None, "m", buffer.getvalue(), ORIENTATION_LANDSCAPE)
        order = []
        worker.doubtful.connect(lambda: order.append("doubtful"))
        worker.done.connect(lambda image: order.append(image))
        worker.run()
        assert order == expected + [generated]  # das Bild der KI bleibt unveraendert


def test_doubtful_check_works_for_portrait_extension():
    original = motif()
    canvas = Image.new("RGB", (400, 600), (90, 90, 90))
    canvas.paste(original, (0, 100))
    canvas.paste(original.crop((0, 0, 400, 1)).resize((400, 100)), (0, 0))  # ohne Kante weitergefuehrt
    canvas.paste(original.crop((0, 399, 400, 400)).resize((400, 100)), (0, 500))
    good = restore_covered(canvas, original, ())
    assert good.image.size == (400, 600) and not good.doubtful
    canvas.paste(ImageOps.flip(original), (0, 100))
    assert restore_covered(canvas, original, ()).doubtful


def test_everything_covered_does_not_fail():
    original = motif()
    result = restore_covered(Image.new("RGB", (600, 400), (0, 255, 0)), original,
                             (MaskShape(SHAPE_RECT, 0, 0, 1, 1),))
    assert result.edge_difference == 0.0 and not result.doubtful


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
    result = extend_masked(client, "m", buffer.getvalue(), ORIENTATION_PORTRAIT, masking).image

    sent = Image.open(io.BytesIO(base64.b64decode(calls[0]["input"][1]["data"]))).convert("RGB")
    assert sent.size == (400, 400)
    assert sent.crop((120, 120, 280, 280)).getcolors(maxcolors=4) is not None  # Flaeche statt Schachbrett
    assert sent.crop((120, 120, 280, 280)) != original.crop((120, 120, 280, 280))
    assert "placeholders" in calls[0]["input"][0]["text"]
    # Die abgedeckte Stelle traegt wieder das Original, der Rest bleibt von der KI
    assert result.crop((120, 220, 280, 380)).tobytes() == original.crop((120, 120, 280, 280)).tobytes()
    assert result.getpixel((50, 300)) == (0, 255, 0)


def test_worker_announces_doubtful_result_before_done(qt_app, monkeypatch):
    from core import cover_generator
    from gui.workers import CoverExtendWorker

    extension = MaskedExtension(Image.new("RGB", (600, 400)), EXTEND_DOUBTFUL_DIFFERENCE + 1)
    monkeypatch.setattr(cover_generator, "extend_masked", lambda *args: extension)
    worker = CoverExtendWorker(None, "m", png_bytes(), ORIENTATION_PORTRAIT, ExtendMasking(()))
    order = []
    worker.doubtful.connect(lambda: order.append("doubtful"))
    worker.done.connect(lambda image: order.append(image))
    worker.run()
    assert order == ["doubtful", extension.image]


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


# --- Pinselzeile ----------------------------------------------------------------------------------

class FakeWorkerSignals:
    class Signal:
        def __init__(self):
            self.slots = []

        def connect(self, slot):
            self.slots.append(slot)

        def emit(self, *args):
            for slot in self.slots:
                slot(*args)

    def __init__(self):
        self.finished, self.failed, self.refused = self.Signal(), self.Signal(), self.Signal()


@pytest.fixture
def bar(qt_app, monkeypatch):
    from gui.activity_bar import ActivityBar

    clock = [100.0]
    monkeypatch.setattr(ActivityBar, "now", staticmethod(lambda: clock[0]))
    widget = ActivityBar()
    widget.clock = clock
    yield widget
    widget._timer.stop()
    widget._done_timer.stop()


def test_activity_stays_hidden_for_short_tasks_and_when_idle(bar):
    assert bar.isHidden()
    worker = FakeWorkerSignals()
    bar.track(worker, "Tags werden gelesen")
    assert bar.isHidden()  # kurze Vorgaenge flackern nicht
    worker.finished.emit()
    assert bar.isHidden() and not bar._timer.isActive() and not bar._done_timer.isActive()


def test_activity_shows_text_and_time_then_done(bar):
    worker = FakeWorkerSignals()
    bar.track(worker, "Rand wird per KI ergänzt")
    bar.clock[0] += 12
    bar.refresh()
    assert not bar.isHidden() and bar.label.text() == "Rand wird per KI ergänzt …" and bar.seconds.text() == "12 s"
    assert bar.stroke.progress is not None
    worker.finished.emit()
    assert bar.label.text() == "Fertig" and bar.stroke.progress is None and bar._done_timer.isActive()
    bar._done_timer.timeout.emit()
    assert bar.isHidden()


def test_activity_does_not_report_done_after_failure(bar):
    worker = FakeWorkerSignals()
    bar.track(worker, "Motive werden erzeugt")
    bar.clock[0] += 5
    bar.refresh()
    worker.failed.emit("kaputt")
    worker.finished.emit()
    assert bar.isHidden() and bar.label.text() != "Fertig"


def test_activity_lists_parallel_tasks_and_ends_with_the_last(bar):
    first, second = FakeWorkerSignals(), FakeWorkerSignals()
    bar.track(first, "Cover werden gesucht")
    bar.clock[0] += 1
    bar.track(second, "KI-Rückseite wird erzeugt")
    bar.refresh()
    assert bar.label.text() == "Cover werden gesucht · KI-Rückseite wird erzeugt …"
    first.finished.emit()
    assert bar.label.text() == "KI-Rückseite wird erzeugt …" and bar._timer.isActive()
    second.finished.emit()
    assert bar.label.text() == "Fertig"


def test_brush_stays_completely_inside_its_area(qt_app):
    """Der Pinsel soll sichtbar sein: An keiner Stelle des Wegs darf er abgeschnitten werden"""
    from PyQt6.QtGui import QColor, QImage
    from gui.activity_bar import BrushStroke

    stroke = BrushStroke()
    for progress in (0.0, 0.1, 0.25, 0.5, 0.75, 0.9, 1.0):
        stroke.set_state(progress, "#89b4fa")
        pixmap = stroke.grab()
        image = pixmap.toImage().convertToFormat(QImage.Format.Format_ARGB32)
        background = QColor(image.pixel(image.width() // 2, 0))
        painted_rows = [y for y in range(image.height())
                        if any(QColor(image.pixel(x, y)) != background for x in range(image.width()))]
        assert painted_rows and painted_rows[0] > 0 and painted_rows[-1] < image.height() - 1, progress
        for y in range(image.height()):
            assert QColor(image.pixel(0, y)) == background, progress  # links nichts abgeschnitten
            assert QColor(image.pixel(image.width() - 1, y)) == background, progress


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
        done = failed = refused = finished = doubtful = Signal()

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

    monkeypatch.setattr(cm, "CoverExtendWorker", Worker)
    monkeypatch.setattr(cm, "CoverMaskDialog", FakeMask)
    dialog._extend_selected()
    assert len(started[0]) == 4  # der erste Versuch geht ohne Abdeckung
    dialog._open_mask_dialog()
    masking = started[1][4]
    assert masking.shapes == (shape,) and masking.effect == EFFECT_FILL
    dialog._open_mask_dialog()  # zweite Ablehnung: Formen bleiben stehen
    assert seen == [[], [shape]]
    dialog._on_extended(Image.new("RGB", (400, 600)))
    assert dialog.selected.label.startswith("KI-Rand (abgedeckt)")

    warned = []
    monkeypatch.setattr(dialog, "_warn_doubtful", lambda: warned.append(True))
    dialog._on_extended(Image.new("RGB", (400, 600)))
    assert not warned  # ohne Hinweis des Workers keine Warnung
    dialog._on_extend_doubtful()
    dialog._on_extended(Image.new("RGB", (400, 600)))
    assert warned and dialog.selected.label.startswith("KI-Rand (abgedeckt)")  # das Bild ist trotzdem da
    dialog.selected = picture
    dialog._extend_selected()  # ein neuer Lauf beginnt wieder ohne Formen
    assert dialog._mask_shapes == []


def test_new_try_after_seam_warning_runs_without_masking(manager, monkeypatch):
    cm, dialog = manager
    picture = cm.Candidate(cm.SECTION_ONLINE, "Deezer", png_bytes((400, 400)), KIND_ONLINE)
    dialog._add_candidate(picture, select=True)
    started, masks = [], []

    class Signal:
        def connect(self, slot):
            pass

    class Worker:
        done = failed = refused = finished = doubtful = Signal()

        def __init__(self, *args):
            started.append(args)

        def start(self):
            pass

        def isRunning(self):
            return False

    class FakeBox:
        Icon, ButtonRole = cm.QMessageBox.Icon, cm.QMessageBox.ButtonRole

        def __init__(self, parent):
            self.buttons = []

        def __getattr__(self, name):
            return lambda *args: None

        def addButton(self, text, role):
            self.buttons.append(text)
            return text

        def exec(self):
            return 0

        def clickedButton(self):
            return self.buttons[0]  # "Neuer Versuch"

    monkeypatch.setattr(cm, "CoverExtendWorker", Worker)
    monkeypatch.setattr(cm, "QMessageBox", FakeBox)
    monkeypatch.setattr(dialog, "_open_mask_dialog", lambda: masks.append(True))
    dialog._extend_selected()
    dialog._on_extend_doubtful()
    dialog._on_extended(Image.new("RGB", (600, 400)))
    assert len(started) == 2 and len(started[1]) == 4 and not masks  # erneut ohne Abdeckung, kein Abdeck-Fenster
    assert dialog.selected.label.startswith("KI-Rand ·")  # das erste Bild bleibt unter "Neu"


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
    assert dialog.effect() == EFFECT_FILL  # Standard, solange nichts anderes gewaehlt wurde
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


# --- KI-Rueckseite: Vorschau, Ablehnung, Abdecken; Rand-Prompt als Backup ---------------------

def test_back_generation_sends_masked_front(qt_app):
    from core.cover_generator import BACK_MASKED_NOTE, GenerationRequest, generate_back_art
    from core.cover_research import CoverBrief, FolderInfo
    request = GenerationRequest(info=FolderInfo(album="Eule"), brief=CoverBrief())
    front = checker((400, 400))
    buffer = io.BytesIO()
    front.save(buffer, "PNG")
    calls = []

    def create(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(output_image=SimpleNamespace(data=png_bytes((400, 600))), output_text="")

    client = SimpleNamespace(interactions=SimpleNamespace(create=create))
    masking = ExtendMasking((MaskShape(SHAPE_RECT, 0.25, 0.25, 0.5, 0.5),), EFFECT_FILL)
    generate_back_art(client, "m", request, buffer.getvalue(), masking=masking)
    generate_back_art(client, "m", request, buffer.getvalue())
    sent = Image.open(io.BytesIO(base64.b64decode(calls[0]["input"][1]["data"]))).convert("RGB")
    assert sent.crop((120, 120, 280, 280)).getcolors(maxcolors=4) is not None  # Flaeche statt Schachbrett
    assert sent.getpixel((10, 10)) == front.getpixel((10, 10))
    assert BACK_MASKED_NOTE in calls[0]["input"][0]["text"]
    assert BACK_MASKED_NOTE not in calls[1]["input"][0]["text"]
    assert calls[1]["input"][1]["data"] == base64.b64encode(buffer.getvalue()).decode("ascii")


def test_back_worker_reports_refusal_separately(qt_app, monkeypatch):
    from core import cover_generator
    from gui.workers import CoverBackGenerateWorker

    def refuse(*args, **kwargs):
        raise ImageRefused("back")

    monkeypatch.setattr(cover_generator, "generate_back_art", refuse)
    worker = CoverBackGenerateWorker(None, "m", None, png_bytes(), "", True)
    seen = []
    worker.refused.connect(lambda text: seen.append(("refused", text)))
    worker.failed.connect(lambda text: seen.append(("failed", text)))
    worker.run()
    assert [kind for kind, _ in seen] == ["refused"] and "Decke Gesichter und Schrift" in seen[0][1]


def test_back_refusal_offers_masking_the_front(manager, monkeypatch):
    cm, dialog = manager
    picture = cm.Candidate(cm.SECTION_ONLINE, "Deezer", png_bytes((600, 400)), KIND_ONLINE)
    dialog._add_candidate(picture, select=True)
    dialog.btn_card_cover.click()
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

    class FakeMask:
        def __init__(self, image, shapes, parent):
            assert image.size == (600, 400)  # die Vorderseite wird abgedeckt

        def exec(self):
            return cm.QDialog.DialogCode.Accepted

        def shapes(self):
            return [shape]

        def effect(self):
            return EFFECT_FILL

    class FakeBox:
        Icon, ButtonRole = cm.QMessageBox.Icon, cm.QMessageBox.ButtonRole

        def __init__(self, parent):
            self.buttons = []

        def __getattr__(self, name):
            return lambda *args: None

        def addButton(self, text, role):
            self.buttons.append(text)
            return text

        def exec(self):
            return 0

        def clickedButton(self):
            return self.buttons[0]

    monkeypatch.setattr(cm, "CoverBackGenerateWorker", Worker)
    monkeypatch.setattr(cm, "CoverMaskDialog", FakeMask)
    monkeypatch.setattr(cm, "QMessageBox", FakeBox)
    dialog._ai_research_done = True  # sonst laeuft zuerst die Recherche
    dialog._generate_ai_back()
    assert started[0][6] is None  # der erste Versuch geht ohne Abdeckung
    dialog._on_back_refused("abgelehnt")
    assert started[1][6].shapes == (shape,) and dialog._back_mask_shapes == [shape]


def test_new_ai_back_is_shown_in_card_tab(manager):
    cm, dialog = manager
    picture = cm.Candidate(cm.SECTION_ONLINE, "Deezer", png_bytes((600, 400), (0, 0, 200)), KIND_ONLINE)
    dialog._add_candidate(picture, select=True)
    dialog.btn_card_cover.click()
    dialog.tabs.setCurrentIndex(1)  # dort sitzt der Knopf "KI-Rückseite erzeugen"
    dialog._back_front = dialog.front_source
    dialog._on_back_generated(Image.new("RGB", (600, 400), (200, 0, 0)))
    assert dialog.side_bar.currentIndex() == 1
    image = dialog.preview.pixmap().toImage()
    assert image.pixelColor(image.width() // 2, image.height() // 2).getRgb()[:3] == (200, 0, 0)
    assert "noch nicht zugewiesen" in dialog.preview_title.text()
    dialog.btn_back_cover.click()
    assert dialog.back_source.candidate is dialog.selected and dialog._pending_back() is None


def test_classic_extend_prompt_stays_available(qt_app):
    from PyQt6.QtCore import QSettings
    from core import ai_settings
    from core.cover_generator import EXTEND_PROMPT_V1, extend_prompt
    assert "panel" in extend_prompt(False)
    QSettings().setValue(ai_settings.SETTINGS_EXTEND_PROMPT, ai_settings.EXTEND_PROMPT_CLASSIC)
    assert extend_prompt(False) == EXTEND_PROMPT_V1.format(shape="wide landscape 3:2")
    assert "Keep the original image completely unchanged" in extend_prompt(True)
    QSettings().setValue(ai_settings.SETTINGS_EXTEND_PROMPT, "unbekannt")
    assert "panel" in extend_prompt(True)
