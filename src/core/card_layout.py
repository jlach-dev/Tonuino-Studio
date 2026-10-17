"""
Kartencover im Scheckkartenformat (ID-1, 85,6 x 54 mm) rendern

Die KI bzw. der Ambient-Blur liefern nur das Motiv (Rohbild ohne Text). Titel und
Ordnernummer setzt dieses Modul per Pillow - lesbar und ohne Tippfehler. Das
Rohbild bleibt gespeichert, damit Titel, Ausrichtung oder Etikettengroesse ohne
neue (kostenpflichtige) Bildgenerierung geaendert werden koennen.
"""

import os
import sys
from dataclasses import dataclass
from typing import List, Optional

from PIL import Image, ImageDraw, ImageFilter, ImageFont, ImageOps

# Druckaufloesung fuer Karten und Druckbogen
DPI = 300

# ISO/IEC 7810 ID-1 (Scheckkarte)
CARD_LONG_MM = 85.6
CARD_SHORT_MM = 54.0

ORIENTATION_PORTRAIT = "portrait"
ORIENTATION_LANDSCAPE = "landscape"


def mm_to_px(mm: float, dpi: int = DPI) -> int:
    return round(mm / 25.4 * dpi)


@dataclass
class CardSpec:
    """Abmessungen einer zu rendernden Karte bzw. eines Etiketts (Hochformat-
    unabhaengig angegeben: long/short; die Ausrichtung entscheidet, was Breite ist).
    bleed_mm: Beschnittzugabe je Seite, safe_mm: Abstand von Text zum Rand."""
    long_mm: float = CARD_LONG_MM
    short_mm: float = CARD_SHORT_MM
    bleed_mm: float = 2.0
    safe_mm: float = 3.0

    def size_mm(self, orientation: str, with_bleed: bool = True):
        extra = 2 * self.bleed_mm if with_bleed else 0.0
        if orientation == ORIENTATION_PORTRAIT:
            return self.short_mm + extra, self.long_mm + extra
        return self.long_mm + extra, self.short_mm + extra

    def size_px(self, orientation: str, with_bleed: bool = True, dpi: int = DPI):
        w, h = self.size_mm(orientation, with_bleed)
        return mm_to_px(w, dpi), mm_to_px(h, dpi)


@dataclass
class CardText:
    """Was per Code auf die Karte gesetzt wird. title darf mehrere Zeilen
    enthalten (eine je Folge, z.B. bei Doppelfolgen); series steht klein
    darueber (Hoerspielreihe, ggf. mit Folgennummer)."""
    title: str = ""
    folder_number: Optional[int] = None
    series: str = ""

    @property
    def paragraphs(self) -> List[str]:
        return [line.strip() for line in self.title.splitlines() if line.strip()]


# Schriftarten: zuerst eine mitgelieferte Schrift (src/resources/card_fonts/*.ttf),
# sonst gaengige fette Systemschriften der drei Plattformen.
_SYSTEM_FONTS = [
    # Windows
    r"C:\Windows\Fonts\seguibl.ttf",
    r"C:\Windows\Fonts\segoeuib.ttf",
    r"C:\Windows\Fonts\arialbd.ttf",
    # macOS
    "/System/Library/Fonts/Supplemental/Arial Rounded Bold.ttf",
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
    "/Library/Fonts/Arial Bold.ttf",
    # Linux
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/TTF/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
]


def _bundled_font_dir() -> str:
    if getattr(sys, "frozen", False):
        base_dir = getattr(sys, "_MEIPASS", os.path.dirname(sys.executable))
        return os.path.join(base_dir, "src", "resources", "card_fonts")
    return os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "resources", "card_fonts")


def find_title_font() -> Optional[str]:
    """Pfad einer fetten TrueType-Schrift fuer den Kartentitel (None = Pillow-Default)"""
    font_dir = _bundled_font_dir()
    if os.path.isdir(font_dir):
        for name in sorted(os.listdir(font_dir)):
            if name.lower().endswith((".ttf", ".otf")):
                return os.path.join(font_dir, name)
    for path in _SYSTEM_FONTS:
        if os.path.exists(path):
            return path
    return None


def _load_font(path: Optional[str], size: int) -> ImageFont.FreeTypeFont:
    if path:
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            pass
    return ImageFont.load_default(size)


def fit_art(art: Image.Image, size) -> Image.Image:
    """Schneidet das Motiv mittig auf das Zielformat zu (keine Verzerrung)"""
    return ImageOps.fit(art.convert("RGB"), size, Image.Resampling.LANCZOS, centering=(0.5, 0.45))


def ambient_blur(cover: Image.Image, size) -> Image.Image:
    """Klassischer TonUINO-Covermaker-Look ohne KI: das (meist quadratische)
    Cover unverzerrt einpassen, den Rest mit einer vergroesserten, unscharfen
    und leicht abgedunkelten Kopie fuellen."""
    cover = cover.convert("RGB")
    background = ImageOps.fit(cover, size, Image.Resampling.LANCZOS)
    background = background.filter(ImageFilter.GaussianBlur(radius=max(size) / 25))
    background = Image.blend(background, Image.new("RGB", size, (0, 0, 0)), 0.25)

    foreground = ImageOps.contain(cover, size, Image.Resampling.LANCZOS)
    x = (size[0] - foreground.width) // 2
    y = (size[1] - foreground.height) // 2
    background.paste(foreground, (x, y))
    return background


def prepare_card_art(image: Image.Image, orientation: str, tolerance: float = 0.15) -> Image.Image:
    """Macht aus einem beliebigen Bild ein Kartenmotiv: passt das
    Seitenverhaeltnis ungefaehr, bleibt es (Zuschnitt beim Rendern);
    sonst - typisch fuer quadratische Album-Cover - Ambient Blur, damit nichts
    Wichtiges abgeschnitten wird."""
    size = CardSpec().size_px(orientation)
    target = size[0] / size[1]
    actual = image.width / image.height
    if abs(actual - target) / target <= tolerance:
        return image.convert("RGB")
    return ambient_blur(image, size)


def album_cover_jpeg(image: Image.Image, size: int = 600) -> bytes:
    """Quadratisches Album-Cover (JPEG) fuer die ID3-Tags"""
    import io
    square = ImageOps.fit(image.convert("RGB"), (size, size), Image.Resampling.LANCZOS)
    buffer = io.BytesIO()
    square.save(buffer, "JPEG", quality=90)
    return buffer.getvalue()


def _wrap_lines(draw: ImageDraw.ImageDraw, text: str, font, max_width: int) -> List[str]:
    """Bricht text wortweise auf max_width um"""
    lines: List[str] = []
    current = ""
    for word in text.split():
        candidate = f"{current} {word}".strip()
        if draw.textlength(candidate, font=font) <= max_width or not current:
            current = candidate
        else:
            lines.append(current)
            current = word
    if current:
        lines.append(current)
    return lines


def _truncate(draw, line: str, font, max_width: int) -> str:
    while line and draw.textlength(line + "…", font=font) > max_width:
        line = line[:-1]
    return line.rstrip() + "…"


def fit_title(draw: ImageDraw.ImageDraw, text: str, font_path: Optional[str],
              max_width: int, max_height: int, max_lines: int, start_size: int, min_size: int = 10):
    """Einzeiliger Sonderfall von fit_text_block (ohne Reihe)"""
    block = fit_text_block(draw, "", [text], font_path, max_width, max_height, max_lines, start_size, min_size)
    return block.font, block.lines, block.line_height


@dataclass
class TextBlock:
    font: object
    lines: List[str]
    line_height: int
    series_font: object = None
    series_lines: Optional[List[str]] = None
    series_line_height: int = 0
    paragraph_starts: frozenset = frozenset()  # Zeilen, mit denen eine weitere Folge beginnt

    @property
    def paragraph_gap(self) -> int:
        return round(self.line_height * 0.35)

    @property
    def height(self) -> int:
        series = len(self.series_lines or []) * self.series_line_height
        gap = round(self.series_line_height * 0.25) if self.series_lines else 0
        paragraphs = sum(1 for i in self.paragraph_starts if 0 < i < len(self.lines)) * self.paragraph_gap
        return series + gap + len(self.lines) * self.line_height + paragraphs


def fit_text_block(draw: ImageDraw.ImageDraw, series: str, paragraphs: List[str], font_path: Optional[str],
                   max_width: int, max_height: int, max_lines: int, start_size: int,
                   min_size: int = 10) -> TextBlock:
    """Groesste Schriftgroesse, mit der Reihe (klein) und alle Titelzeilen in
    die Box passen; jede Folge beginnt in einer neuen Zeile. Passt es selbst in
    der Mindestgroesse nicht, wird gekuerzt (…)."""
    size = start_size
    while True:
        font = _load_font(font_path, size)
        series_size = max(min_size, round(size * 0.58))
        series_font = _load_font(font_path, series_size)
        lines, starts = [], set()
        for paragraph in paragraphs:
            starts.add(len(lines))
            lines.extend(_wrap_lines(draw, paragraph, font, max_width))
        series_lines = _wrap_lines(draw, series, series_font, max_width)[:1] if series.strip() else []
        block = TextBlock(font, lines, round(size * 1.15), series_font, series_lines, round(series_size * 1.2),
                          frozenset(starts))
        fits_width = all(draw.textlength(line, font=font) <= max_width for line in lines)
        if (len(lines) <= max_lines and block.height <= max_height and fits_width) or size <= min_size:
            break
        size -= 2

    if block.series_lines and draw.textlength(block.series_lines[0], font=series_font) > max_width:
        block.series_lines[0] = _truncate(draw, block.series_lines[0], series_font, max_width)
    if len(block.lines) > max_lines:
        block.lines = block.lines[:max_lines]
        block.lines[-1] = _truncate(draw, block.lines[-1], block.font, max_width)
    return block


def _draw_centered(draw, y: float, line: str, font, width: int, shadow: int, fill):
    x = (width - draw.textlength(line, font=font)) / 2
    draw.text((x + shadow, y + shadow), line, font=font, fill=(0, 0, 0, 160))
    draw.text((x, y), line, font=font, fill=fill)


def render_card(art: Image.Image, text: CardText, orientation: str,
                spec: Optional[CardSpec] = None, with_bleed: bool = True, dpi: int = DPI) -> Image.Image:
    """Setzt Motiv, Textband (Reihe + Titel) und optionale Ordnernummer zur fertigen Karte zusammen."""
    spec = spec or CardSpec()
    size = spec.size_px(orientation, with_bleed, dpi)
    card = fit_art(art, size).convert("RGBA")

    bleed = mm_to_px(spec.bleed_mm, dpi) if with_bleed else 0
    safe = bleed + mm_to_px(spec.safe_mm, dpi)
    width, height = size
    paragraphs = text.paragraphs

    if paragraphs or text.series.strip():
        portrait = orientation == ORIENTATION_PORTRAIT
        draw = ImageDraw.Draw(card)
        block = fit_text_block(
            draw, text.series.strip(), paragraphs, find_title_font(), width - 2 * safe,
            max_height=round(height * (0.40 if portrait else 0.46)),
            max_lines=(5 if portrait else 3) if len(paragraphs) > 1 else (3 if portrait else 2),
            start_size=mm_to_px(6.0 if len(paragraphs) <= 1 else 4.6, dpi))

        # Textband unten: weicher Verlauf von transparent nach dunkel, damit
        # weisse Schrift auf jedem Motiv lesbar bleibt; waechst mit dem Text
        band_height = max(round(height * (0.30 if portrait else 0.36)), round(block.height * 1.7) + safe)
        band_height = min(band_height, height)
        gradient = Image.new("L", (1, band_height))
        for y in range(band_height):
            gradient.putpixel((0, y), round(205 * (y / band_height) ** 1.3))
        overlay = Image.new("RGBA", (width, band_height), (0, 0, 0, 255))
        overlay.putalpha(gradient.resize((width, band_height)))
        card.alpha_composite(overlay, (0, height - band_height))

        draw = ImageDraw.Draw(card)
        y = height - safe - block.height
        shadow = max(2, block.line_height // 18)
        for line in block.series_lines or []:
            _draw_centered(draw, y, line, block.series_font, width, max(1, shadow // 2), (255, 255, 255, 215))
            y += block.series_line_height
        if block.series_lines:
            y += round(block.series_line_height * 0.25)
        for index, line in enumerate(block.lines):
            if index and index in block.paragraph_starts:
                y += block.paragraph_gap
            _draw_centered(draw, y, line, block.font, width, shadow, (255, 255, 255, 255))
            y += block.line_height

    if text.folder_number is not None:
        # Kleines Nummern-Badge oben rechts - hilft Eltern beim Zuordnen
        draw = ImageDraw.Draw(card)
        badge_font = _load_font(find_title_font(), mm_to_px(2.6, dpi))
        label = f"{text.folder_number:02d}"
        pad = mm_to_px(0.9, dpi)
        text_w = draw.textlength(label, font=badge_font)
        box_h = mm_to_px(2.6, dpi) + 2 * pad
        box_w = round(text_w) + 2 * pad
        x1 = width - safe
        y0 = safe
        draw.rounded_rectangle((x1 - box_w, y0, x1, y0 + box_h), radius=box_h // 2, fill=(255, 255, 255, 200))
        draw.text((x1 - box_w + pad, y0 + box_h / 2), label, font=badge_font, fill=(40, 40, 40, 255), anchor="lm")

    return card.convert("RGB")


BACK_MODE_NONE = ""
BACK_MODE_IMAGE = "image"  # Original-Rueckseite (bzw. gewaehltes Bild) ohne Text
BACK_MODE_TRACKLIST = "tracklist"  # Bild als Hintergrund, Trackliste per Code


def _fit_list_font(draw, lines: List[str], font_path: Optional[str], max_width: int, max_height: int,
                   start_size: int, min_size: int):
    """Groesste Schriftgroesse, mit der alle Zeilen (ohne Umbruch) in die Box passen"""
    size = start_size
    while True:
        font = _load_font(font_path, size)
        line_height = round(size * 1.3)
        if (len(lines) * line_height <= max_height
                and all(draw.textlength(line, font=font) <= max_width for line in lines)) or size <= min_size:
            return font, line_height
        size -= 1


def render_back_card(art: Image.Image, orientation: str, heading: str = "", lines: Optional[List[str]] = None,
                     spec: Optional[CardSpec] = None, with_bleed: bool = True, dpi: int = DPI) -> Image.Image:
    """Rueckseite: das Bild randabfallend; mit lines kommt ein halbtransparentes
    Feld (so hoch wie der Inhalt) mit Ueberschrift und Trackliste darueber.
    Die Schrift passt sich an, zu lange Zeilen werden gekuerzt (max. ~25 Zeilen)."""
    spec = spec or CardSpec()
    size = spec.size_px(orientation, with_bleed, dpi)
    card = fit_art(art, size).convert("RGBA")
    lines = [line.strip() for line in (lines or []) if line.strip()][:25]
    heading = heading.strip()
    if not lines and not heading:
        return card.convert("RGB")

    bleed = mm_to_px(spec.bleed_mm, dpi) if with_bleed else 0
    safe = bleed + mm_to_px(spec.safe_mm, dpi)
    width, height = size
    pad = mm_to_px(2.0, dpi)
    max_width = width - 2 * safe - 2 * pad
    draw = ImageDraw.Draw(card)
    font_path = find_title_font()

    # 1. Textblock bemessen
    head_font = head_height = None
    if heading:
        head_font, head_height = _fit_list_font(draw, [heading], font_path, max_width, height,
                                                start_size=mm_to_px(3.4, dpi), min_size=mm_to_px(2.2, dpi))
        heading = _truncate(draw, heading, head_font, max_width) \
            if draw.textlength(heading, font=head_font) > max_width else heading
        head_height = round(head_height * 1.15)
    available = height - 2 * safe - 2 * pad - (head_height or 0)
    font = line_height = None
    if lines:
        font, line_height = _fit_list_font(draw, lines, font_path, max_width, available,
                                           start_size=mm_to_px(2.6, dpi), min_size=mm_to_px(1.4, dpi))
        lines = lines[:max(1, available // line_height)]
    content_height = (head_height or 0) + (len(lines) * line_height if lines else 0)

    # 2. Feld nur so hoch wie der Inhalt
    panel = (safe, safe, width - safe, min(height - safe, safe + 2 * pad + content_height))
    overlay = Image.new("RGBA", size, (0, 0, 0, 0))
    ImageDraw.Draw(overlay).rounded_rectangle(panel, radius=mm_to_px(2.5, dpi), fill=(0, 0, 0, 165))
    card.alpha_composite(overlay)

    # 3. Text
    draw = ImageDraw.Draw(card)
    x, y = panel[0] + pad, panel[1] + pad
    if heading:
        draw.text((x, y), heading, font=head_font, fill=(255, 255, 255, 255))
        y += head_height
    for line in lines:
        text = line
        if draw.textlength(text, font=font) > max_width:
            text = _truncate(draw, text, font, max_width)
        draw.text((x, y), text, font=font, fill=(235, 235, 235, 255))
        y += line_height
    return card.convert("RGB")
