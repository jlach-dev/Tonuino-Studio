"""
A4-Druckbogen (PDF) fuer Kartencover auf Etikettenboegen

Der Bogen wird als 300-dpi-Bild aufgebaut und von Pillow mit exakter
Seitengroesse als PDF gespeichert - so landet jedes Etikett beim Druck in
"Originalgroesse / 100 %" millimetergenau an seiner Position.
"""

from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from PIL import Image, ImageDraw, ImageOps

from core.card_layout import (
    DPI, CardSpec, ORIENTATION_PORTRAIT, mm_to_px, _load_font, find_title_font,
)

A4_MM = (210.0, 297.0)


@dataclass(frozen=True)
class LabelTemplate:
    """Raster eines Etikettenbogens. Alle Werte in mm, bezogen auf A4 hoch.
    Die Etiketten liegen im Querformat (label_w > label_h) auf dem Bogen;
    Hochformat-Cover werden fuer den Druck um 90 Grad gedreht."""
    key: str
    name: str
    label_w: float
    label_h: float
    columns: int
    rows: int
    margin_left: float
    margin_top: float
    gap_x: float
    gap_y: float
    # Beschnitt, um den das Motiv ueber das Etikett hinausgeht (0 bei
    # vorgestanzten Boegen, damit nichts auf das Nachbaretikett druckt)
    bleed: float = 0.0
    cut_marks: bool = False
    verified: bool = False
    note: str = ""

    @property
    def per_sheet(self) -> int:
        return self.columns * self.rows

    def card_spec(self) -> CardSpec:
        return CardSpec(long_mm=self.label_w, short_mm=self.label_h, bleed_mm=self.bleed, safe_mm=3.0)

    def slot_origin_mm(self, slot: int) -> Tuple[float, float]:
        """Linke obere Ecke (ohne Beschnitt) des Etiketts Nr. slot (0-basiert,
        zeilenweise von links oben)"""
        row, col = divmod(slot, self.columns)
        x = self.margin_left + col * (self.label_w + self.gap_x)
        y = self.margin_top + row * (self.label_h + self.gap_y)
        return x, y


def _centered(key, name, label_w, label_h, columns, rows, gap_x, gap_y, **kwargs) -> LabelTemplate:
    margin_left = (A4_MM[0] - columns * label_w - (columns - 1) * gap_x) / 2
    margin_top = (A4_MM[1] - rows * label_h - (rows - 1) * gap_y) / 2
    return LabelTemplate(key, name, label_w, label_h, columns, rows,
                         round(margin_left, 2), round(margin_top, 2), gap_x, gap_y, **kwargs)


LABEL_TEMPLATES: Dict[str, LabelTemplate] = {t.key: t for t in [
    # Im TonUINO-Forum am haeufigsten genutzt. Die Bogenraender sind nicht
    # offiziell veroeffentlicht - Raster zentriert angenommen, per Testseite
    # pruefen und ggf. mit dem Druck-Offset korrigieren.
    _centered("herma5028", "HERMA 5028 (83,8 × 50,8 mm, 10/Bogen)",
              83.8, 50.8, 2, 5, gap_x=12.7, gap_y=0.0,
              note="Bogenränder nicht verifiziert – bitte Testseite drucken"),
    # Townstix A4-8: 2 x 4 Etiketten ohne Rand und Abstand (Masse laut Hersteller,
    # townstix.com/templates). Die meisten Drucker drucken nicht bis an den
    # Papierrand - die aeusseren Etiketten bekommen dann einen weissen Streifen.
    LabelTemplate("townstix_a4_8", "Townstix A4-8 (105 × 74,25 mm, 8/Bogen)",
                  105.0, 74.25, 2, 4, 0.0, 0.0, 0.0, 0.0,
                  note="Randlos: Drucker ohne Randlosdruck lassen außen einen weißen Streifen"),
    # Vollflaechiges A4-Etikett/Fotopapier, selbst ausschneiden
    _centered("iso_cut", "Zum Ausschneiden mit Schnittmarken (Kartenformat)",
              85.6, 53.98, 2, 4, gap_x=10.0, gap_y=10.0, bleed=2.0, cut_marks=True, verified=True,
              note="Zum Selbstausschneiden mit 2 mm Beschnitt"),
]}

DEFAULT_TEMPLATE = "herma5028"

# --- Kartenformat und Lage auf dem Etikett ------------------------------------------

CARD_ISO_MM = (85.6, 53.98)  # ISO/IEC 7810 ID-1, das Format der RFID-Karten
ALIGN_START, ALIGN_CENTER, ALIGN_END = "start", "center", "end"  # links/oben, mittig, rechts/unten
ALIGNMENTS = (ALIGN_START, ALIGN_CENTER, ALIGN_END)
CUT_GAP_MM = 10.0  # Abstand der Karten auf Boegen zum Selbstausschneiden
CUT_MIN_MARGIN_MM = 7.0


@dataclass(frozen=True)
class Placement:
    """Wie gross ein Cover gedruckt wird und wo es im Etikett liegt.
    card_w/card_h: Kartenformat quer in mm. fill_label: Cover auf Etikettengroesse
    statt Kartengroesse (alte Arbeitsweise, etwa fuer Aufkleber). Ist die Karte
    groesser als das Etikett, wird sie am Etikettenrand abgeschnitten."""
    card_w: float = CARD_ISO_MM[0]
    card_h: float = CARD_ISO_MM[1]
    fill_label: bool = False
    align_x: str = ALIGN_START
    align_y: str = ALIGN_START

    def size_mm(self, template: "LabelTemplate") -> Tuple[float, float]:
        if self.fill_label:
            return template.label_w, template.label_h
        return self.card_w, self.card_h

    def card_spec(self, template: "LabelTemplate") -> CardSpec:
        w, h = self.size_mm(template)
        return CardSpec(long_mm=max(w, h), short_mm=min(w, h), bleed_mm=template.bleed, safe_mm=3.0)


def _offset(free: float, align: str) -> float:
    return 0.0 if align == ALIGN_START else free if align == ALIGN_END else free / 2


def layout_for(template: LabelTemplate, placement: Placement) -> LabelTemplate:
    """Boegen zum Selbstausschneiden bekommen ein Raster in Kartengroesse
    (so viele Karten, wie auf A4 passen); vorgestanzte Boegen bleiben, wie sie sind."""
    if not template.cut_marks or placement.fill_label:
        return template
    w, h = placement.card_w + 2 * template.bleed, placement.card_h + 2 * template.bleed
    columns = max(1, int((A4_MM[0] - 2 * CUT_MIN_MARGIN_MM + CUT_GAP_MM) // (w + CUT_GAP_MM)))
    rows = max(1, int((A4_MM[1] - 2 * CUT_MIN_MARGIN_MM + CUT_GAP_MM) // (h + CUT_GAP_MM)))
    gap = CUT_GAP_MM + 2 * template.bleed
    return _centered(template.key, template.name, placement.card_w, placement.card_h, columns, rows,
                     gap_x=gap, gap_y=gap, bleed=template.bleed, cut_marks=True, verified=template.verified,
                     note=template.note)


def card_box_mm(template: LabelTemplate, placement: Placement, slot: int) -> Tuple[float, float, float, float]:
    """(x, y, Breite, Hoehe) der Karte (ohne Beschnitt) auf dem Bogen"""
    x, y = template.slot_origin_mm(slot)
    w, h = placement.size_mm(template)
    return (x + _offset(template.label_w - w, placement.align_x),
            y + _offset(template.label_h - h, placement.align_y), w, h)


@dataclass
class SheetItem:
    """Ein zu druckendes Kartenmotiv (bereits fertig gerendert, ohne Drehung)"""
    image: Image.Image
    orientation: str


def _slot_image(item: SheetItem, size_mm: Tuple[float, float], bleed: float, dpi: int) -> Image.Image:
    """Bringt das Cover auf Kartengroesse inkl. Beschnitt; Hochformat wird
    gedreht, weil die Karten quer auf dem Bogen liegen."""
    image = item.image
    if item.orientation == ORIENTATION_PORTRAIT:
        image = image.rotate(90, expand=True)
    w = mm_to_px(size_mm[0] + 2 * bleed, dpi)
    h = mm_to_px(size_mm[1] + 2 * bleed, dpi)
    # fit statt resize: weicht das Seitenverhaeltnis ab, wird minimal
    # beschnitten statt verzerrt (besser: Karte passend zum Etikett rendern)
    return ImageOps.fit(image.convert("RGB"), (w, h), Image.Resampling.LANCZOS)


def _draw_cut_marks(draw: ImageDraw.ImageDraw, x: int, y: int, w: int, h: int, dpi: int):
    """Schnittmarken ausserhalb des Beschnitts an allen vier Ecken"""
    length = mm_to_px(3, dpi)
    offset = mm_to_px(1, dpi)
    color = (0, 0, 0)
    width = max(1, dpi // 150)
    for cx in (x, x + w):
        for cy in (y, y + h):
            sx = -1 if cx == x else 1
            sy = -1 if cy == y else 1
            draw.line((cx + sx * offset, cy, cx + sx * (offset + length), cy), fill=color, width=width)
            draw.line((cx, cy + sy * offset, cx, cy + sy * (offset + length)), fill=color, width=width)


def build_sheets(items: List[SheetItem], template: LabelTemplate, start_slot: int = 0,
                 offset_mm: Tuple[float, float] = (0.0, 0.0), dpi: int = DPI,
                 outline: bool = False, placement: Optional[Placement] = None) -> List[Image.Image]:
    """Verteilt die Cover ab start_slot auf so viele A4-Seiten wie noetig.
    start_slot erlaubt das Weiterverwenden angebrochener Boegen (nur Seite 1).
    outline zeichnet die Etikettenumrisse (Testseite), dazu die Lage der Karte.
    placement: Kartenformat und Ausrichtung (Standard: ISO-Karte links oben)."""
    placement = placement or Placement()
    template = layout_for(template, placement)
    page_size = (mm_to_px(A4_MM[0], dpi), mm_to_px(A4_MM[1], dpi))
    pages: List[Image.Image] = []
    slot = max(0, min(start_slot, template.per_sheet - 1))
    page: Optional[Image.Image] = None

    def new_page():
        return Image.new("RGB", page_size, (255, 255, 255))

    queue = list(items) if items else []
    if outline and not queue:
        page = new_page()
        pages.append(page)

    for item in queue:
        if page is None or slot >= template.per_sheet:
            if page is not None:
                slot = 0
            page = new_page()
            pages.append(page)
        x_mm, y_mm, w_mm, h_mm = card_box_mm(template, placement, slot)
        bleed = template.bleed
        image = _slot_image(item, (w_mm, h_mm), bleed, dpi)
        x = mm_to_px(x_mm - bleed + offset_mm[0], dpi)
        y = mm_to_px(y_mm - bleed + offset_mm[1], dpi)
        # nie ueber das eigene Etikett (samt Beschnitt) hinaus drucken
        lx, ly = template.slot_origin_mm(slot)
        clip = (mm_to_px(lx - bleed + offset_mm[0], dpi), mm_to_px(ly - bleed + offset_mm[1], dpi),
                mm_to_px(lx + template.label_w + bleed + offset_mm[0], dpi),
                mm_to_px(ly + template.label_h + bleed + offset_mm[1], dpi))
        left, top = max(x, clip[0]), max(y, clip[1])
        right, bottom = min(x + image.width, clip[2]), min(y + image.height, clip[3])
        if right > left and bottom > top:
            page.paste(image.crop((left - x, top - y, right - x, bottom - y)), (left, top))
        if template.cut_marks:
            draw = ImageDraw.Draw(page)
            _draw_cut_marks(draw, mm_to_px(x_mm + offset_mm[0], dpi), mm_to_px(y_mm + offset_mm[1], dpi),
                            mm_to_px(w_mm, dpi), mm_to_px(h_mm, dpi), dpi)
        slot += 1

    if outline:
        for page in pages:
            draw = ImageDraw.Draw(page)
            font = _load_font(find_title_font(), mm_to_px(3, dpi))
            for s in range(template.per_sheet):
                x_mm, y_mm = template.slot_origin_mm(s)
                x0 = mm_to_px(x_mm + offset_mm[0], dpi)
                y0 = mm_to_px(y_mm + offset_mm[1], dpi)
                x1 = x0 + mm_to_px(template.label_w, dpi)
                y1 = y0 + mm_to_px(template.label_h, dpi)
                draw.rectangle((x0, y0, x1, y1), outline=(220, 0, 0), width=max(1, dpi // 100))
                cx, cy, cw, ch = card_box_mm(template, placement, s)
                if (cw, ch) != (template.label_w, template.label_h):  # Lage der Karte im Etikett
                    draw.rectangle((mm_to_px(cx + offset_mm[0], dpi), mm_to_px(cy + offset_mm[1], dpi),
                                    mm_to_px(cx + cw + offset_mm[0], dpi), mm_to_px(cy + ch + offset_mm[1], dpi)),
                                   outline=(0, 90, 200), width=max(1, dpi // 150))
                draw.text(((x0 + x1) / 2, (y0 + y1) / 2), str(s + 1), font=font, fill=(220, 0, 0), anchor="mm")
            draw.text((mm_to_px(10, dpi), mm_to_px(4, dpi)),
                      f"Testseite {template.name} – in Originalgröße (100 %) drucken und gegen den Bogen halten",
                      font=_load_font(find_title_font(), mm_to_px(2.5, dpi)), fill=(0, 0, 0))
    return pages


def save_pdf(pages: List[Image.Image], path: str, dpi: int = DPI):
    if not pages:
        raise ValueError("Keine Seiten zum Speichern")
    first, *rest = pages
    first.save(path, "PDF", resolution=float(dpi), save_all=True, append_images=rest)
