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
    # Vollflaechiges A4-Etikett/Fotopapier, selbst ausschneiden
    _centered("iso_cut", "ISO-Karte 85,6 × 54 mm, Schnittmarken (8/Bogen)",
              85.6, 54.0, 2, 4, gap_x=10.0, gap_y=10.0, bleed=2.0, cut_marks=True, verified=True,
              note="Zum Selbstausschneiden mit 2 mm Beschnitt"),
]}

DEFAULT_TEMPLATE = "herma5028"


@dataclass
class SheetItem:
    """Ein zu druckendes Kartenmotiv (bereits fertig gerendert, ohne Drehung)"""
    image: Image.Image
    orientation: str


def _slot_image(item: SheetItem, template: LabelTemplate, dpi: int) -> Image.Image:
    """Bringt das Cover auf Etikettengroesse inkl. Beschnitt; Hochformat wird
    gedreht, weil die Etiketten quer auf dem Bogen liegen."""
    image = item.image
    if item.orientation == ORIENTATION_PORTRAIT:
        image = image.rotate(90, expand=True)
    w = mm_to_px(template.label_w + 2 * template.bleed, dpi)
    h = mm_to_px(template.label_h + 2 * template.bleed, dpi)
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
                 outline: bool = False) -> List[Image.Image]:
    """Verteilt die Cover ab start_slot auf so viele A4-Seiten wie noetig.
    start_slot erlaubt das Weiterverwenden angebrochener Boegen (nur Seite 1).
    outline zeichnet die Etikettenumrisse (Testseite)."""
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
        x_mm, y_mm = template.slot_origin_mm(slot)
        x = mm_to_px(x_mm - template.bleed + offset_mm[0], dpi)
        y = mm_to_px(y_mm - template.bleed + offset_mm[1], dpi)
        page.paste(_slot_image(item, template, dpi), (x, y))
        if template.cut_marks:
            draw = ImageDraw.Draw(page)
            _draw_cut_marks(draw, mm_to_px(x_mm + offset_mm[0], dpi), mm_to_px(y_mm + offset_mm[1], dpi),
                            mm_to_px(template.label_w, dpi), mm_to_px(template.label_h, dpi), dpi)
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
