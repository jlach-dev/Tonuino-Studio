"""
Bereiche eines Bildes unkenntlich machen, bevor es an die KI geht

Der Nutzer markiert Schriftzuege und Gesichter von Hand (Rechteck oder Ellipse). Die Formen
werden relativ zur Bildgroesse gespeichert, damit sie unabhaengig von der Anzeigegroesse sind.
"""

from dataclasses import dataclass
from typing import Iterable, List, Tuple

from PIL import Image, ImageDraw, ImageFilter

SHAPE_RECT, SHAPE_ELLIPSE = "rect", "ellipse"
EFFECT_BLUR, EFFECT_FILL = "blur", "fill"
EFFECTS = [
    ("Weichzeichnen", EFFECT_BLUR),
    ("Farbfläche", EFFECT_FILL),
]
BLUR_SHARE = 1 / 20  # Radius als Anteil der langen Bildseite: Gesichter und Schrift sind danach nicht mehr erkennbar


@dataclass(frozen=True)
class MaskShape:
    kind: str
    x: float
    y: float
    width: float
    height: float

    def box(self, size: Tuple[int, int]) -> Tuple[int, int, int, int]:
        """Pixelkoordinaten im Bild, immer mindestens ein Pixel gross"""
        image_w, image_h = size
        left = max(0, min(image_w - 1, round(self.x * image_w)))
        top = max(0, min(image_h - 1, round(self.y * image_h)))
        right = max(left + 1, min(image_w, round((self.x + self.width) * image_w)))
        bottom = max(top + 1, min(image_h, round((self.y + self.height) * image_h)))
        return left, top, right, bottom


def shape_mask(size: Tuple[int, int], shapes: Iterable[MaskShape]) -> Image.Image:
    """Maske in Bildgroesse: 255 innerhalb der Formen"""
    mask = Image.new("L", size, 0)
    draw = ImageDraw.Draw(mask)
    for shape in shapes:
        left, top, right, bottom = shape.box(size)
        box = (left, top, right - 1, bottom - 1)
        if shape.kind == SHAPE_ELLIPSE:
            draw.ellipse(box, fill=255)
        else:
            draw.rectangle(box, fill=255)
    return mask


def apply_masks(image: Image.Image, shapes: List[MaskShape], effect: str = EFFECT_BLUR) -> Image.Image:
    """Das Bild mit den unkenntlich gemachten Bereichen. Das Original bleibt unveraendert."""
    image = image.convert("RGB")
    if not shapes:
        return image.copy()
    if effect == EFFECT_FILL:
        result = image.copy()
        for shape in shapes:  # Farbe aus dem Original, nicht aus einer schon gefuellten Nachbarform
            single = shape_mask(image.size, [shape])
            color = image.crop(shape.box(image.size)).resize((1, 1), Image.Resampling.BOX).getpixel((0, 0))
            result.paste(color, (0, 0, *image.size), single)
        return result
    blurred = image.filter(ImageFilter.GaussianBlur(radius=max(image.size) * BLUR_SHARE))
    result = image.copy()
    result.paste(blurred, (0, 0), shape_mask(image.size, shapes))
    return result
