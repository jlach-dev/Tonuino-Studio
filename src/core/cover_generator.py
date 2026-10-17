"""
Motiv fuer Kartencover erzeugen: per Gemini-Bildmodell ("Nano Banana") oder
ohne KI als Ambient-Blur aus dem vorhandenen Cover.

Die KI liefert bewusst nur das Bild ohne Schrift - den Titel setzt
core/card_layout.py, weil generierter Text oft Tippfehler hat.
"""

import base64
import io
import logging
from dataclasses import dataclass, field
from typing import List

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

# Obergrenze fuer Vorlagenbilder pro Anfrage. Nano Banana 2.1 erlaubt laut
# Doku bis zu 4 Figuren- und 10 Objekt-Referenzen; 6 haelt Anfragen klein.
MAX_REFERENCE_IMAGES = 6

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
    # Vom Nutzer im Cover-Manager markierte Vorlagen (rohe Bilddaten). Leer =
    # Rueckfall auf brief.reference_image (Batch-Modus)
    reference_images: List[bytes] = field(default_factory=list)

    def references(self) -> List[bytes]:
        if not self.use_reference:
            return []
        refs = self.reference_images or ([self.brief.reference_image] if self.brief.reference_image else [])
        return refs[:MAX_REFERENCE_IMAGES]


def build_prompt(req: GenerationRequest) -> str:
    style = STYLE_PRESETS.get(req.style, req.style)
    shape = "tall portrait (2:3)" if req.orientation == ORIENTATION_PORTRAIT else "wide landscape (3:2)"
    content = req.brief_text.strip() or req.brief.as_text()
    from core.card_text import describe_work
    work = describe_work(req.info, req.brief)
    lines = [
        f"Create cover artwork for a children's RFID music card (credit-card size, {shape} format).",
    ]
    subject = work.series or work.album_tag or "unknown"
    lines.append(f"Audio play / series: {subject}" + (f" by {work.artist}" if work.artist else ""))
    if work.episodes:
        label = "Episodes on this card" if len(work.episodes) > 1 else "Episode"
        lines.append(f"{label}: " + "; ".join(work.episodes))
    if work.album_tag and not work.from_track_titles and work.album_tag != subject:
        lines.append(f"Album tag: {work.album_tag}")
    if content:
        lines.append(f"Background information (German):\n{content}")
    if req.extra.strip():
        lines.append(f"Additional wish (German): {req.extra.strip()}")
    lines += [
        f"Style: {style}.",
        ("Composition: one clear main scene that hints at both episodes; " if len(work.episodes) > 1 else
         "Composition: one clear, instantly recognizable main subject, readable at 5 cm size; ") +
        "full-bleed artwork reaching all edges; keep the lower third calm and simple "
        "because a title will be placed there later; no frame, no border.",
        "IMPORTANT: absolutely no text, letters, numbers, logos or watermarks in the image.",
        "Child-friendly, cheerful, no scary or violent content.",
    ]
    refs = req.references()
    if len(refs) == 1:
        lines.append("Use the attached original cover as reference for characters, mood and colors, "
                     "but recompose it as a new illustration for the card format, without its text.")
    elif refs:
        lines.append(f"Use the {len(refs)} attached reference images for the characters' appearance, "
                     "mood and colors (keep the characters recognizable), but compose one new illustration "
                     "for the card format, without any of their text.")
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

    refs = req.references()
    if refs:
        model_input = [{"type": "text", "text": prompt}] + [
            {"type": "image", "data": base64.b64encode(ref).decode("ascii"), "mime_type": _sniff_mime(ref)}
            for ref in refs
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
    refs = req.reference_images or ([req.brief.reference_image] if req.brief.reference_image else [])
    if not refs:
        raise ValueError("Kein Cover vorhanden – für Ambient Blur wird ein Album-Cover benötigt")
    with Image.open(io.BytesIO(refs[0])) as img:
        img.load()
        cover = img.convert("RGB")
    return ambient_blur(cover, CardSpec().size_px(req.orientation))


def generate_art(source: str, req: GenerationRequest, client=None, model: str = "") -> Image.Image:
    if source == SOURCE_BLUR:
        return blur_art(req)
    if client is None:
        raise ValueError("Kein Gemini-API-Key hinterlegt")
    return generate_ai_art(client, model, req)
