"""
Kartencover im Scheckkartenformat (ID-1, 85,6 x 54 mm) rendern

Die KI bzw. der Ambient-Blur liefern nur das Motiv (Rohbild ohne Text). Titel und
Ordnernummer setzt dieses Modul per Pillow - lesbar und ohne Tippfehler. Das
Rohbild bleibt gespeichert, damit Titel, Ausrichtung oder Etikettengroesse ohne
neue (kostenpflichtige) Bildgenerierung geaendert werden koennen.
"""

import os
import sys
from dataclasses import asdict, dataclass
from typing import Dict, List, Optional

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
class TextStyle:
    """Schrift fuer einen Textbereich der Karte (Reihe bzw. Titel)"""
    family: str = ""  # "" = Standardschrift (gebuendelt bzw. System)
    size_mm: float = 0.0  # 0 = automatisch (so gross wie es passt)
    bold: bool = True
    color: str = "#ffffff"

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data) -> "TextStyle":
        if not isinstance(data, dict):
            return cls()
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})

    def rgba(self, alpha: int = 255) -> tuple:
        value = self.color.lstrip("#")
        try:
            r, g, b = (int(value[i:i + 2], 16) for i in (0, 2, 4))
        except (ValueError, IndexError):
            r, g, b = 255, 255, 255
        return r, g, b, alpha


@dataclass
class CardText:
    """Was per Code auf die Karte gesetzt wird. title darf mehrere Zeilen
    enthalten (eine je Folge, z.B. bei Doppelfolgen); series steht klein
    darueber (Hoerspielreihe, ggf. mit Folgennummer)."""
    title: str = ""
    folder_number: Optional[int] = None
    series: str = ""
    title_style: Optional[TextStyle] = None
    series_style: Optional[TextStyle] = None

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


_FONT_DIRS = [
    r"C:\Windows\Fonts",
    os.path.join(os.path.expanduser("~"), "AppData", "Local", "Microsoft", "Windows", "Fonts"),
    "/System/Library/Fonts", "/System/Library/Fonts/Supplemental", "/Library/Fonts",
    os.path.join(os.path.expanduser("~"), "Library", "Fonts"),
    "/usr/share/fonts", "/usr/local/share/fonts", os.path.join(os.path.expanduser("~"), ".fonts"),
]
_font_index: Optional[Dict[str, Dict[str, str]]] = None


def _scan_fonts() -> Dict[str, Dict[str, str]]:
    """family (klein) -> {stil (klein): pfad}; einmalig je Prozess"""
    global _font_index
    if _font_index is not None:
        return _font_index
    index: Dict[str, Dict[str, str]] = {}
    for directory in [_bundled_font_dir()] + _FONT_DIRS:
        if not os.path.isdir(directory):
            continue
        for root, _dirs, files in os.walk(directory):
            for name in files:
                if not name.lower().endswith((".ttf", ".otf")):
                    continue
                path = os.path.join(root, name)
                try:
                    family, style = ImageFont.truetype(path, 10).getname()
                except Exception:
                    continue
                index.setdefault((family or "").lower(), {})[(style or "").lower()] = path
    _font_index = index
    return index


def find_font_file(family: str, bold: bool = True) -> Optional[str]:
    """Schriftdatei zu einem Familiennamen (z.B. "Comic Sans MS"), bevorzugt fett"""
    styles = _scan_fonts().get((family or "").strip().lower())
    if not styles:
        return None
    wanted = ["bold", "fett", "semibold", "demibold", "black", "heavy"] if bold else ["regular", "normal", "book", "standard"]
    for key in wanted:
        for style, path in styles.items():
            if (style == key or style.startswith(key + " ")) and "italic" not in style:
                return path
    if bold:
        for style, path in styles.items():
            if "bold" in style and "italic" not in style:
                return path
    for style, path in styles.items():
        if "italic" not in style and "oblique" not in style:
            return path
    return next(iter(styles.values()))


def resolve_font(style: Optional["TextStyle"]) -> Optional[str]:
    """Schriftdatei fuer einen Textstil; ohne Familie die Standardschrift"""
    if style is not None and style.family.strip():
        path = find_font_file(style.family, style.bold)
        if path:
            return path
    return find_title_font()


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


FORMAT_TOLERANCE = 0.05  # darunter genuegt der minimale Zuschnitt beim Rendern


def fits_card_format(image: Image.Image, orientation: str, tolerance: float = FORMAT_TOLERANCE) -> bool:
    """Hat das Bild (ungefaehr) schon das Seitenverhaeltnis der Karte? Dann
    braucht es weder Ambient Blur noch eine KI-Randergaenzung."""
    size = CardSpec().size_px(orientation)
    target = size[0] / size[1]
    actual = image.width / image.height
    return abs(actual - target) / target <= tolerance


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
                   min_size: int = 10, title_style: Optional[TextStyle] = None,
                   series_style: Optional[TextStyle] = None, dpi: int = DPI) -> TextBlock:
    """Groesste Schriftgroesse, mit der Reihe (klein) und alle Titelzeilen in
    die Box passen; jede Folge beginnt in einer neuen Zeile. Passt es selbst in
    der Mindestgroesse nicht, wird gekuerzt (…). Feste Groessen aus den Stilen
    (size_mm > 0) werden nicht verkleinert."""
    title_path = resolve_font(title_style) if title_style else font_path
    series_path = resolve_font(series_style) if series_style else font_path
    fixed_title = mm_to_px(title_style.size_mm, dpi) if title_style and title_style.size_mm > 0 else 0
    fixed_series = mm_to_px(series_style.size_mm, dpi) if series_style and series_style.size_mm > 0 else 0
    size = fixed_title or start_size
    if fixed_title:
        min_size = fixed_title
    while True:
        font = _load_font(title_path, size)
        series_size = fixed_series or max(min_size, round(size * 0.58))
        series_font = _load_font(series_path, series_size)
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
            start_size=mm_to_px(6.0 if len(paragraphs) <= 1 else 4.6, dpi),
            title_style=text.title_style, series_style=text.series_style, dpi=dpi)
        title_color = (text.title_style or TextStyle()).rgba()
        series_color = (text.series_style or TextStyle()).rgba(215)

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
            _draw_centered(draw, y, line, block.series_font, width, max(1, shadow // 2), series_color)
            y += block.series_line_height
        if block.series_lines:
            y += round(block.series_line_height * 0.25)
        for index, line in enumerate(block.lines):
            if index and index in block.paragraph_starts:
                y += block.paragraph_gap
            _draw_centered(draw, y, line, block.font, width, shadow, title_color)
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
BACK_MODE_IMAGE = "image"  # Original-Rueckseite bzw. gewaehltes Bild
BACK_MODE_AI = "ai"  # per Bildmodell passend zur Vorderseite erzeugt (optional mit Wunsch)
BACK_MODE_BLUR = "blur"  # Vorderseite weichgezeichnet und abgedunkelt
BACK_MODE_GRADIENT = "gradient"  # Farbverlauf aus den Coverfarben
BACK_MODE_TRACKLIST = "tracklist"  # (alt) Bild + Trackliste; heute: Flag back_tracklist


def blur_background(front: Image.Image, size) -> Image.Image:
    """Vorderseite als ruhiger Hintergrund: stark weichgezeichnet und abgedunkelt"""
    background = ImageOps.fit(front.convert("RGB"), size, Image.Resampling.LANCZOS)
    background = background.filter(ImageFilter.GaussianBlur(radius=max(size) / 18))
    return Image.blend(background, Image.new("RGB", size, (0, 0, 0)), 0.45)


def dominant_colors(image: Image.Image, count: int = 2):
    """Die count haeufigsten Farben (ohne fast Weiss/Schwarz, falls moeglich)"""
    small = image.convert("RGB").resize((64, 64))
    quantized = small.quantize(colors=8, method=Image.Quantize.MEDIANCUT)
    palette = quantized.getpalette()[:8 * 3]
    counts = sorted(quantized.getcolors(), reverse=True)
    colors = [tuple(palette[index * 3:index * 3 + 3]) for _n, index in counts]
    usable = [c for c in colors if 25 < sum(c) / 3 < 235] or colors
    while len(usable) < count:
        usable.append(usable[-1] if usable else (60, 60, 90))
    return usable[:count]


def gradient_background(front: Image.Image, size) -> Image.Image:
    """Senkrechter Verlauf aus den zwei dominanten Coverfarben (dunklere unten)"""
    top, bottom = dominant_colors(front, 2)
    if sum(top) < sum(bottom):
        top, bottom = bottom, top
    bottom = tuple(round(c * 0.6) for c in bottom)
    strip = Image.new("RGB", (1, size[1]))
    for y in range(size[1]):
        t = y / max(1, size[1] - 1)
        strip.putpixel((0, y), tuple(round(top[i] * (1 - t) + bottom[i] * t) for i in range(3)))
    return strip.resize(size)


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
