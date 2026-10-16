from PIL import Image

from core import card_store
from core.card_layout import (
    CardSpec, CardText, ORIENTATION_LANDSCAPE, ORIENTATION_PORTRAIT, ambient_blur, mm_to_px, render_card,
)
from core.sd_card import SDCard
from core.sheet_pdf import LABEL_TEMPLATES, SheetItem, build_sheets, save_pdf


def make_card(root, layout):
    for folder, files in layout.items():
        (root / folder).mkdir()
        for name in files:
            (root / folder / name).write_bytes(name.encode())
    card = SDCard(str(root))
    card.scan()
    return card


def test_card_size_at_300_dpi():
    spec = CardSpec(bleed_mm=0)
    assert spec.size_px(ORIENTATION_LANDSCAPE) == (1011, 638)
    assert spec.size_px(ORIENTATION_PORTRAIT) == (638, 1011)
    # mit 2 mm Beschnitt je Seite
    assert CardSpec().size_px(ORIENTATION_LANDSCAPE) == (mm_to_px(89.6), mm_to_px(58.0))


def test_render_card_keeps_size_for_any_art_ratio():
    art = Image.new("RGB", (1000, 1000), (10, 120, 200))
    for orientation in (ORIENTATION_PORTRAIT, ORIENTATION_LANDSCAPE):
        card = render_card(art, CardText("Ein sehr langer Hörspieltitel mit vielen Wörtern", 7), orientation)
        assert card.size == CardSpec().size_px(orientation)


def test_ambient_blur_fills_target():
    blurred = ambient_blur(Image.new("RGB", (500, 500), (200, 0, 0)), (638, 1011))
    assert blurred.size == (638, 1011)


def test_purge_keeps_card_files_in_numbered_folders_only(tmp_path):
    card = make_card(tmp_path, {
        "01": ["001.mp3", "card.png", "card_art_raw.png", "card.json", "notes.txt"],
        "mp3": ["0001.mp3", "card.png"],
    })
    assert card.folders[1].card_path.endswith("card.png")
    assert card.preview_purge().foreign_items_in_folders == 2  # notes.txt, mp3/card.png

    card.purge()

    assert sorted(p.name for p in (tmp_path / "01").iterdir()) == [
        "001.mp3", "card.json", "card.png", "card_art_raw.png"]
    assert sorted(p.name for p in (tmp_path / "mp3").iterdir()) == ["0001.mp3"]


def test_card_store_roundtrip(tmp_path):
    art = Image.new("RGB", (800, 1200), (0, 200, 100))
    meta = card_store.CardMeta(title="Bibi", orientation=ORIENTATION_PORTRAIT, source="blur")
    card_store.save_card(str(tmp_path), art, meta, folder_number=3)

    assert card_store.has_card(str(tmp_path))
    assert card_store.load_meta(str(tmp_path)).title == "Bibi"
    assert card_store.load_raw_art(str(tmp_path)).size == (800, 1200)

    card_store.delete_card(str(tmp_path))
    assert not any(tmp_path.iterdir())


def test_sheet_layout_fits_on_a4_and_paginates(tmp_path):
    for template in LABEL_TEMPLATES.values():
        x, y = template.slot_origin_mm(template.per_sheet - 1)
        assert template.margin_left >= 0 and template.margin_top >= 0
        assert x + template.label_w <= 210 and y + template.label_h <= 297

    template = LABEL_TEMPLATES["herma5028"]
    items = [SheetItem(Image.new("RGB", (638, 1011), (255, 0, 0)), ORIENTATION_PORTRAIT) for _ in range(12)]
    pages = build_sheets(items, template, start_slot=4, dpi=100)
    # 6 Etiketten auf Seite 1 (ab Slot 5), 6 auf Seite 2
    assert len(pages) == 2

    pdf = tmp_path / "bogen.pdf"
    save_pdf(pages, str(pdf), dpi=100)
    assert pdf.read_bytes().startswith(b"%PDF")


def test_test_page_without_items():
    pages = build_sheets([], LABEL_TEMPLATES["herma5028"], outline=True, dpi=100)
    assert len(pages) == 1
