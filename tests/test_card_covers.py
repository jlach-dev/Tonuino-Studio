import pytest
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


def test_card_files_are_replaced_atomically(tmp_path, monkeypatch):
    """Scheitert das Schreiben (z.B. Karte gezogen), bleibt die alte Datei heil"""
    import os
    from core import card_store
    from core.card_store import CardMeta

    art = Image.new("RGB", (400, 600), (200, 100, 50))
    card_store.save_card(str(tmp_path), art, CardMeta(title="Alt"), 1)
    old_meta = (tmp_path / "card.json").read_bytes()

    def broken_replace(src, dst):
        raise OSError("Karte entfernt")

    monkeypatch.setattr(card_store.os, "replace", broken_replace)
    with pytest.raises(OSError):
        card_store.save_card(str(tmp_path), art, CardMeta(title="Neu"), 1)
    monkeypatch.undo()
    assert (tmp_path / "card.json").read_bytes() == old_meta
    assert card_store.load_meta(str(tmp_path)).title == "Alt"
    assert os.path.exists(tmp_path / "card.png")


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


def test_card_text_from_tags_series_and_double_episodes():
    from core.card_text import derive_card_text
    from core.cover_research import FolderInfo

    assert derive_card_text(FolderInfo(
        albums=["Folge 39: Fall 77: Die verschleppte Schleppe/Fall 78: Der gefiederte Vergessling"],
        artist="Die Fuchsbande")) == (
        "Die Fuchsbande · Folge 39", "Fall 77: Die verschleppte Schleppe\nFall 78: Der gefiederte Vergessling")
    assert derive_card_text(FolderInfo(
        albums=["Bibi Blocksberg - Folge 1: Hexen gibt es doch", "Bibi Blocksberg - Folge 2: Der Superbesen"],
        artist="Bibi Blocksberg")) == ("Bibi Blocksberg", "Folge 1: Hexen gibt es doch\nFolge 2: Der Superbesen")
    assert derive_card_text(FolderInfo(albums=["AC/DC Live"], artist="AC/DC")) == ("AC/DC", "AC/DC Live")
    # Podcast-Download: Album-Tag ist nur der Feed, Reihe und Folgen stehen im Tracktitel
    feed = FolderInfo(album="MausHörspiel lang", artist="Otfried Preußler", titles=[
        "Die kleine Hexe (1/2): Heia Walpurgisnacht | MausHörspiel lang (07.12.2024)",
        "Die kleine Hexe (2/2): Der größte Streich | MausHörspiel lang (07.12.2024)"])
    assert derive_card_text(feed) == ("Die kleine Hexe", "1. Heia Walpurgisnacht\n2. Der größte Streich")
    # dieselbe Folge in zwei Teilen -> nur ein Titel, ohne Nummer
    assert derive_card_text(FolderInfo(titles=["Bibi - Folge 12: Der Superbesen (Teil 1)",
                                               "Bibi - Folge 12: Der Superbesen (Teil 1)"])) == (
        "Bibi", "Der Superbesen (Teil 1)")


def test_multi_episode_text_fits_card():
    art = Image.new("RGB", (800, 1200), (10, 120, 200))
    text = CardText("Fall 77: Die verschleppte Schleppe\nFall 78: Der gefiederte Vergessling\nFall 79: Noch einer",
                    5, "Die Fuchsbande · Folge 39")
    for orientation in (ORIENTATION_PORTRAIT, ORIENTATION_LANDSCAPE):
        assert render_card(art, text, orientation).size == CardSpec().size_px(orientation)


def test_describe_work_basis_and_title():
    from core.card_text import describe_work
    from core.cover_research import FolderInfo

    feed = describe_work(FolderInfo(album="MausHörspiel lang", artist="Otfried Preußler", titles=[
        "Die kleine Hexe (1/2): Heia Walpurgisnacht | MausHörspiel lang",
        "Die kleine Hexe (2/2): Der größte Streich | MausHörspiel lang"]))
    assert feed.title == "Die kleine Hexe · 1. Heia Walpurgisnacht, 2. Der größte Streich"
    assert feed.from_track_titles and "Feed" in feed.basis

    verified = describe_work(FolderInfo(album="Folge 39: Fall 77: Die verschleppte Schleppe",
                                        artist="Die Fuchsbande", tags_verified=True))
    assert verified.series == "Die Fuchsbande · Folge 39"
    assert verified.episodes == ["Fall 77: Die verschleppte Schleppe"]
    assert "MusicBrainz" in verified.basis

    assert describe_work(FolderInfo()).title == ""


def test_card_text_keeps_series_word_and_ignores_single_track_albums():
    from core.card_text import derive_card_text, main_albums
    from core.cover_research import FolderInfo

    # "Eule" ist Reihe UND erstes Wort des Titels - darf nicht abgeschnitten werden
    info = FolderInfo(artist="Eule",
                      albums=["Eule findet den Beat (Musik-Hörspiel)", "Eule findet den Beat - Auf Europatour",
                              "Eule findet den Beat - mit Gefühl - Die Songs"],
                      album_counts={"Eule findet den Beat (Musik-Hörspiel)": 13,
                                    "Eule findet den Beat - Auf Europatour": 1,
                                    "Eule findet den Beat - mit Gefühl - Die Songs": 1})
    assert main_albums(info) == ["Eule findet den Beat (Musik-Hörspiel)"]
    assert derive_card_text(info) == ("Eule", "Eule findet den Beat (Musik-Hörspiel)")

    # zwei Folgen mit je mehreren Tracks bleiben zwei Zeilen
    two = FolderInfo(artist="Bibi Blocksberg", albums=["Bibi Blocksberg - Folge 1: A", "Bibi Blocksberg - Folge 2: B"],
                     album_counts={"Bibi Blocksberg - Folge 1: A": 4, "Bibi Blocksberg - Folge 2: B": 5})
    assert derive_card_text(two) == ("Bibi Blocksberg", "Folge 1: A\nFolge 2: B")



def test_townstix_a4_8_fills_the_sheet_without_gaps():
    template = LABEL_TEMPLATES["townstix_a4_8"]
    assert template.per_sheet == 8 and template.slot_origin_mm(0) == (0.0, 0.0)
    assert template.slot_origin_mm(3) == (105.0, 74.25)
    x, y = template.slot_origin_mm(7)
    assert (x + template.label_w, y + template.label_h) == (210.0, 297.0)


def test_cover_is_printed_at_card_size_and_aligned_in_the_label():
    from core.sheet_pdf import ALIGN_CENTER, ALIGN_END, Placement, card_box_mm
    template = LABEL_TEMPLATES["townstix_a4_8"]
    assert card_box_mm(template, Placement(), 3) == (105.0, 74.25, 85.6, 53.98)  # links oben buendig
    x, y, w, h = card_box_mm(template, Placement(align_x=ALIGN_END, align_y=ALIGN_CENTER), 0)
    assert (round(x + w, 2), round(y, 3)) == (105.0, round((74.25 - 53.98) / 2, 3))
    assert card_box_mm(template, Placement(fill_label=True), 0) == (0.0, 0.0, 105.0, 74.25)
    item = SheetItem(Image.new("RGB", (1011, 638), (255, 0, 0)), ORIENTATION_LANDSCAPE)
    page = build_sheets([item], template, dpi=100)[0]
    px = mm_to_px(85.6, 100)
    assert page.getpixel((px - 2, 5)) == (255, 0, 0) and page.getpixel((px + 2, 5)) == (255, 255, 255)
    assert page.getpixel((5, mm_to_px(53.98, 100) + 2)) == (255, 255, 255)


def test_card_larger_than_label_is_clipped_and_cut_sheets_follow_the_card_format():
    from core.sheet_pdf import Placement, layout_for
    herma = LABEL_TEMPLATES["herma5028"]
    item = SheetItem(Image.new("RGB", (1011, 638), (255, 0, 0)), ORIENTATION_LANDSCAPE)
    page = build_sheets([item], herma, dpi=100)[0]
    x, y = herma.slot_origin_mm(0)
    # nichts ragt unter das Etikett, obwohl die Karte hoeher ist
    assert page.getpixel((mm_to_px(x + 10, 100), mm_to_px(y + herma.label_h, 100) + 2)) == (255, 255, 255)
    cut = layout_for(LABEL_TEMPLATES["iso_cut"], Placement(card_w=88.0, card_h=58.0))
    assert (cut.label_w, cut.label_h) == (88.0, 58.0) and cut.per_sheet == 8
    assert layout_for(LABEL_TEMPLATES["iso_cut"], Placement(card_w=90.0, card_h=60.0)).per_sheet == 3  # 1 x 3
    x, y = cut.slot_origin_mm(cut.per_sheet - 1)
    assert cut.margin_left >= 0 and x + cut.label_w <= 210 and y + cut.label_h <= 297


def test_marks_can_be_switched_and_stay_on_their_own_label():
    from core.sheet_pdf import Placement, registration_marks_mm
    item = SheetItem(Image.new("RGB", (1011, 638), (255, 0, 0)), ORIENTATION_LANDSCAPE)

    def dark(page):
        return page.convert("L").histogram()[0]

    # Bogen zum Ausschneiden: Marken wie bisher, abschaltbar
    cut = LABEL_TEMPLATES["iso_cut"]
    assert dark(build_sheets([item], cut, dpi=100)[0]) > 0
    assert dark(build_sheets([item], cut, dpi=100, marks=False)[0]) == 0

    # vorgestanzt: ohne Haken keine Marken, mit Haken nur im eigenen Etikett
    herma = LABEL_TEMPLATES["herma5028"]
    assert dark(build_sheets([item], herma, dpi=100)[0]) == 0
    page = build_sheets([item], herma, dpi=100, marks=True)[0]
    assert dark(page) > 0
    x, y = herma.slot_origin_mm(1)  # Nachbaretikett bleibt frei von Schnittmarken
    region = page.crop((mm_to_px(x, 100), mm_to_px(y, 100),
                        mm_to_px(x + herma.label_w, 100), mm_to_px(y + herma.label_h, 100)))
    assert dark(region) == 0

    # Townstix randlos: kein Platz am Rand, also keine Passmarken
    assert registration_marks_mm(LABEL_TEMPLATES["townstix_a4_8"]) == []
    page = build_sheets([item], LABEL_TEMPLATES["townstix_a4_8"], dpi=100, marks=True,
                        placement=Placement(fill_label=True))[0]
    assert dark(page) == 0  # Karte fuellt das Etikett, Marken lagen ausserhalb
