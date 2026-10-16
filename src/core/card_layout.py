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
    """Was per Code auf die Karte gesetzt wird"""
    title: str = ""
    folder_number: Optional[int] = None


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


def fit_title(draw: ImageDraw.ImageDraw, text: str, font_path: Optional[str],
              max_width: int, max_height: int, max_lines: int, start_size: int, min_size: int = 10):
    """Groesste Schriftgroesse, mit der der Titel in max_lines Zeilen in die
    Box passt. Gibt (font, lines, line_height) zurueck; passt es selbst in der
    Mindestgroesse nicht, wird die letzte Zeile mit ... gekuerzt."""
    size = start_size
    while True:
        font = _load_font(font_path, size)
        lines = _wrap_lines(draw, text, font, max_width)
        line_height = round(size * 1.15)
        fits_width = all(draw.textlength(line, font=font) <= max_width for line in lines)
        if (len(lines) <= max_lines and len(lines) * line_height <= max_height and fits_width) or size <= min_size:
            break
        size -= 2

    if len(lines) > max_lines:
        lines = lines[:max_lines]
        last = lines[-1]
        while last and draw.textlength(last + "…", font=font) > max_width:
            last = last[:-1]
        lines[-1] = last.rstrip() + "…"
    return font, lines, line_height


def render_card(art: Image.Image, text: CardText, orientation: str,
                spec: Optional[CardSpec] = None, with_bleed: bool = True, dpi: int = DPI) -> Image.Image:
    """Setzt Motiv, Titelband und optionale Ordnernummer zur fertigen Karte zusammen."""
    spec = spec or CardSpec()
    size = spec.size_px(orientation, with_bleed, dpi)
    card = fit_art(art, size).convert("RGBA")

    bleed = mm_to_px(spec.bleed_mm, dpi) if with_bleed else 0
    safe = bleed + mm_to_px(spec.safe_mm, dpi)
    width, height = size

    if text.title.strip():
        # Titelband unten: weicher Verlauf von transparent nach dunkel, damit
        # weisse Schrift auf jedem Motiv lesbar bleibt
        portrait = orientation == ORIENTATION_PORTRAIT
        band_height = round(height * (0.30 if portrait else 0.36))
        gradient = Image.new("L", (1, band_height))
        for y in range(band_height):
            gradient.putpixel((0, y), round(200 * (y / band_height) ** 1.4))
        overlay = Image.new("RGBA", (width, band_height), (0, 0, 0, 255))
        overlay.putalpha(gradient.resize((width, band_height)))
        card.alpha_composite(overlay, (0, height - band_height))

        draw = ImageDraw.Draw(card)
        max_width = width - 2 * safe
        max_height = round(band_height * 0.62)
        font, lines, line_height = fit_title(
            draw, text.title.strip(), find_title_font(), max_width, max_height,
            max_lines=3 if portrait else 2, start_size=mm_to_px(6.0, dpi))

        y = height - safe - len(lines) * line_height
        shadow = max(2, line_height // 18)
        for line in lines:
            line_width = draw.textlength(line, font=font)
            x = (width - line_width) / 2
            draw.text((x + shadow, y + shadow), line, font=font, fill=(0, 0, 0, 160))
            draw.text((x, y), line, font=font, fill=(255, 255, 255, 255))
            y += line_height

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
