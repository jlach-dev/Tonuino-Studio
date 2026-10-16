"""
Motiv fuer Kartencover erzeugen: per Gemini-Bildmodell ("Nano Banana") oder
ohne KI als Ambient-Blur aus dem vorhandenen Cover.

Die KI liefert bewusst nur das Bild ohne Schrift - den Titel setzt
core/card_layout.py, weil generierter Text oft Tippfehler hat.
"""

import base64
import io
import logging
from dataclasses import dataclass

from PIL import Image

from core.card_layout import CardSpec, ORIENTATION_PORTRAIT, ambient_blur
from core.cover_research import CoverBrief, FolderInfo

log = logging.getLogger(__name__)

# Stilvorgaben fuer den Bild-Prompt (Anzeigename -> englische Prompt-Beschreibung)
STYLE_PRESETS = {
    "Kinderbuch-Illustration": "a warm, friendly children's picture-book illustration, soft shapes, "
                               "clean outlines, gentle shading",
    "Aquarell": "a soft watercolor painting with visible paper texture and gentle color washes",
    "Knete / 3D": "a cute claymation-style 3D scene with soft lighting, like handmade plasticine figures",
    "Flach & farbenfroh": "a bold flat vector illustration with bright, saturated colors and simple shapes",
    "Original-Cover-Stil": "the same artistic style, characters and color world as the reference cover image",
}
DEFAULT_STYLE = "Kinderbuch-Illustration"

SOURCE_AI = "ai"
SOURCE_BLUR = "blur"

# Grobe Kostenschaetzung je Bild (USD) fuer den Hinweis vor Batch-Laeufen
ESTIMATED_COST_PER_IMAGE = 0.07


@dataclass
class GenerationRequest:
    info: FolderInfo
    brief: CoverBrief
    brief_text: str = ""  # vom Nutzer ggf. bearbeitete Recherche
    style: str = DEFAULT_STYLE
    extra: str = ""
    orientation: str = ORIENTATION_PORTRAIT
    use_reference: bool = True


def build_prompt(req: GenerationRequest) -> str:
    style = STYLE_PRESETS.get(req.style, req.style)
    shape = "tall portrait (2:3)" if req.orientation == ORIENTATION_PORTRAIT else "wide landscape (3:2)"
    content = req.brief_text.strip() or req.brief.as_text()
    lines = [
        f"Create cover artwork for a children's RFID music card (credit-card size, {shape} format).",
        f"Album / audio play: {req.info.album or 'unknown'}" + (f" by {req.info.artist}" if req.info.artist else ""),
    ]
    if content:
        lines.append(f"Background information (German):\n{content}")
    if req.extra.strip():
        lines.append(f"Additional wish (German): {req.extra.strip()}")
    lines += [
        f"Style: {style}.",
        "Composition: one clear, instantly recognizable main subject, readable at 5 cm size; "
        "full-bleed artwork reaching all edges; keep the lower third calm and simple "
        "because a title will be placed there later; no frame, no border.",
        "IMPORTANT: absolutely no text, letters, numbers, logos or watermarks in the image.",
        "Child-friendly, cheerful, no scary or violent content.",
    ]
    if req.use_reference and req.brief.reference_image:
        lines.append("Use the attached original cover as reference for characters, mood and colors, "
                     "but recompose it as a new illustration for the card format, without its text.")
    return "\n\n".join(lines)


def _sniff_mime(data: bytes) -> str:
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return "image/jpeg"


def generate_ai_art(client, model: str, req: GenerationRequest, image_size: str = "2K") -> Image.Image:
    """Ein Motiv vom Gemini-Bildmodell (Interactions-API)"""
    prompt = build_prompt(req)
    aspect_ratio = "2:3" if req.orientation == ORIENTATION_PORTRAIT else "3:2"

    if req.use_reference and req.brief.reference_image:
        model_input = [
            {"type": "text", "text": prompt},
            {"type": "image", "data": base64.b64encode(req.brief.reference_image).decode("ascii"),
             "mime_type": _sniff_mime(req.brief.reference_image)},
        ]
    else:
        model_input = prompt

    interaction = client.interactions.create(
        model=model,
        input=model_input,
        response_format={"type": "image", "aspect_ratio": aspect_ratio, "image_size": image_size},
    )
    image = getattr(interaction, "output_image", None)
    if image is None or not getattr(image, "data", None):
        text = getattr(interaction, "output_text", "") or ""
        raise RuntimeError("Das Modell hat kein Bild geliefert" + (f": {text[:300]}" if text else ""))
    data = image.data if isinstance(image.data, bytes) else base64.b64decode(image.data)
    with Image.open(io.BytesIO(data)) as img:
        img.load()
        return img.convert("RGB")


def blur_art(req: GenerationRequest) -> Image.Image:
    """Motiv ohne KI aus dem Referenz-Cover (Ambient Blur)"""
    if not req.brief.reference_image:
        raise ValueError("Kein Cover vorhanden – für Ambient Blur wird ein Album-Cover benötigt")
    with Image.open(io.BytesIO(req.brief.reference_image)) as img:
        img.load()
        cover = img.convert("RGB")
    return ambient_blur(cover, CardSpec().size_px(req.orientation))


def generate_art(source: str, req: GenerationRequest, client=None, model: str = "") -> Image.Image:
    if source == SOURCE_BLUR:
        return blur_art(req)
    if client is None:
        raise ValueError("Kein Gemini-API-Key hinterlegt")
    return generate_ai_art(client, model, req)
