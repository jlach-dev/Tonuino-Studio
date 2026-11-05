"""
Motiv fuer Kartencover erzeugen: per Gemini-Bildmodell ("Nano Banana") oder
ohne KI als Ambient-Blur aus dem vorhandenen Cover.

Die KI liefert bewusst nur das Bild ohne Schrift - den Titel setzt
core/card_layout.py, weil generierter Text oft Tippfehler hat.
"""

import base64
import io
import logging
import re
from dataclasses import dataclass, field
from typing import List, Optional

from PIL import Image

from core.ai_settings import get_image_size
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
    # Serie (core/series.py): Anker-Bilder gehen immer zuerst mit (Figuren-Slots),
    # die Stilbibel wird an den Prompt angehaengt
    series_name: str = ""
    series_bible: str = ""
    anchor_images: List[bytes] = field(default_factory=list)

    def anchors(self) -> List[bytes]:
        return self.anchor_images[:MAX_REFERENCE_IMAGES]

    def references(self) -> List[bytes]:
        """Anker zuerst, dann die Vorlagen des Nutzers; zusammen hoechstens
        MAX_REFERENCE_IMAGES (Anker gewinnen), Dubletten nur einmal"""
        anchors = self.anchors()
        refs: List[bytes] = []
        if self.use_reference:
            refs = self.reference_images or ([self.brief.reference_image] if self.brief.reference_image else [])
        result = list(anchors)
        for ref in refs:
            if ref not in result and len(result) < MAX_REFERENCE_IMAGES:
                result.append(ref)
        return result

    @property
    def has_series(self) -> bool:
        return bool(self.anchor_images or self.series_bible.strip())


def series_prompt_lines(req: GenerationRequest, extra_refs: int) -> List[str]:
    """Prompt-Zeilen fuer Serien-Konsistenz: Stilbibel und Anker-Hinweis"""
    lines = []
    anchors = len(req.anchors())
    if req.series_bible.strip():
        label = f" for the series \"{req.series_name}\"" if req.series_name else ""
        lines.append(f"Series style bible{label} (German, binding for characters, style and colors):\n"
                     f"{req.series_bible.strip()}")
    if anchors:
        text = (f"The first {anchors} attached image(s) are the series reference images (character sheet or "
                "earlier covers of the same series): use exactly the same characters, illustration style and "
                "palette as the reference images, so this card is instantly recognizable as part of the series.")
        if extra_refs:
            text += (f" The remaining {extra_refs} attached image(s) show this episode's original cover: take "
                     "subject, mood and composition hints from them, without their text.")
        lines.append(text)
    elif req.series_bible.strip():
        lines.append("Keep exactly the same characters, illustration style and palette as described in the "
                     "style bible, so the card is recognizable as part of the series.")
    return lines


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
    anchors = len(req.anchors())
    if req.has_series:
        lines += series_prompt_lines(req, len(refs) - anchors)
    elif len(refs) == 1:
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


# Google nennt bei einer Ablehnung meist keinen Grund ("unspecified policy reason"). Die Gruende
# sind deshalb Erfahrungswerte, die Tipps richten sich danach, was gerade erzeugt werden sollte.
CONTEXT_MOTIF, CONTEXT_EXTEND, CONTEXT_BACK, CONTEXT_SHEET = "motif", "extend", "back", "sheet"
REFUSED_SHORT = "von Google abgelehnt"
REFUSAL_HELP = {
    CONTEXT_MOTIF: (
        ["Eine KI-Vorlage ist ein bekanntes Cover oder zeigt geschützte Figuren oder Logos.",
         "Titel oder Recherche nennen bekannte Figuren, die nachgezeichnet werden sollen.",
         "Auf einer Vorlage sind Kinder oder echte Personen zu sehen."],
        ["Nimm „Als KI-Vorlage“ von den Bildern zurück und versuche es ohne Vorlage.",
         "Beschreibe unter „Eigener Wunsch“ eigene Figuren und Szenen, ohne bekannte Namen.",
         "Oder weise das Album-Cover mit „Als Vorderseite“ zu: Den Rand ergänzt die App dann ohne KI als Ambient Blur."]),
    CONTEXT_EXTEND: (
        ["Das Bild ist ein bekanntes Cover oder zeigt geschützte Figuren, Logos oder Schriftzüge.",
         "Auf dem Bild sind Kinder oder echte Personen zu sehen."],
        ["Weise das Bild direkt mit „Als Vorderseite“ zu: Den Rand ergänzt die App dann ohne KI als Ambient Blur.",
         "Oder erzeuge unter „Eigener Wunsch“ ein eigenes Motiv, ohne dieses Bild als Vorlage."]),
    CONTEXT_BACK: (
        ["Die Vorderseite wird als Vorlage mitgeschickt und zeigt vermutlich geschützte Figuren oder Logos.",
         "Der KI-Wunsch nennt bekannte Figuren oder Marken."],
        ["Wähle für die Rückseite „Ambient Blur der Vorderseite (ohne KI)“.",
         "Oder nimm eine selbst erzeugte Vorderseite, und beschreibe unter „KI-Wunsch“ eine Szene ohne bekannte Namen."]),
    CONTEXT_SHEET: (
        ["Die Anker-Bilder sind bekannte Cover oder zeigen geschützte Figuren.",
         "Die Stilbibel nennt bekannte Figuren oder Marken."],
        ["Entferne bekannte Cover aus den Ankern.",
         "Beschreibe die Figuren in der Stilbibel mit eigenen Worten, ohne Namen."]),
}
REFUSAL_BATCH_HINT = ("Google nennt meist keinen Grund. Häufig liegt es an bekannten Figuren, Logos oder Covern. "
                      "Für diese Ordner hilft der Cover-Manager: dort ohne Vorlage, mit eigenem Wunsch "
                      "oder mit dem Album-Cover und „Als Vorderseite“, das läuft ohne KI.")


class ImageRefused(RuntimeError):
    """Der Anbieter hat das Bild abgelehnt. str() erklaert es samt Tipps, short ist fuer Listen."""
    short = REFUSED_SHORT

    def __init__(self, context: str, answer: str = ""):
        reasons, tips = REFUSAL_HELP[context]
        parts = ["Google hat dieses Bild abgelehnt und nennt meist keinen genauen Grund. "
                 "Das ist kein Fehler der App.",
                 "Häufige Gründe:\n" + "\n".join(f"• {reason}" for reason in reasons),
                 "Das kannst du tun:\n" + "\n".join(f"• {tip}" for tip in tips)]
        if answer:
            parts.append(f"Antwort von Google: {answer[:300]}")
        super().__init__("\n\n".join(parts))


def request_image(client, context: str, **request) -> Image.Image:
    """Bild vom Modell holen. Abgelehnt wird auf zwei Wegen: als API-Fehler (content_blocked)
    oder als Antwort ohne Bild."""
    try:
        interaction = client.interactions.create(**request)
    except Exception as e:
        if "content_blocked" in str(e).lower():
            # Der Filter greift vor dem Modell, mehr als diesen Text gibt es dann nicht
            message = re.search(r"""['"]message['"]:\s*['"](.+?)['"]\s*[,}]""", str(e))
            raise ImageRefused(context, message.group(1) if message else "") from e
        raise
    image = getattr(interaction, "output_image", None)
    if image is None or not getattr(image, "data", None):
        raise ImageRefused(context, getattr(interaction, "output_text", "") or "")
    data = image.data if isinstance(image.data, bytes) else base64.b64decode(image.data)
    with Image.open(io.BytesIO(data)) as img:
        img.load()
        return img.convert("RGB")


def generate_ai_art(client, model: str, req: GenerationRequest, image_size: str = "") -> Image.Image:
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

    return request_image(
        client, CONTEXT_MOTIF,
        model=model,
        input=model_input,
        response_format={"type": "image", "aspect_ratio": aspect_ratio,
                         "image_size": image_size or get_image_size()},
    )


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


EXTEND_PROMPT = (
    "Extend (outpaint) the attached image to a {shape} format. Keep the original image completely unchanged "
    "and centered at its full size; only continue the background, colors and textures naturally beyond its "
    "edges so the result is one seamless picture. Do not add new text, letters, logos, characters or objects. "
    "If you cannot create this image, return no image and instead state the specific reason in one short "
    "German sentence."
)


def extend_to_card(client, model: str, image_bytes: bytes, orientation: str, image_size: str = "") -> Image.Image:
    """Original-Cover/-Rueckseite per Bildmodell auf das Kartenformat erweitern
    (statt zu beschneiden oder Ambient Blur). Ein API-Aufruf, ohne Recherche."""
    portrait = orientation == ORIENTATION_PORTRAIT
    prompt = EXTEND_PROMPT.format(shape="tall portrait 2:3" if portrait else "wide landscape 3:2")
    return request_image(
        client, CONTEXT_EXTEND,
        model=model,
        input=[{"type": "text", "text": prompt},
               {"type": "image", "data": base64.b64encode(image_bytes).decode("ascii"),
                "mime_type": _sniff_mime(image_bytes)}],
        response_format={"type": "image", "aspect_ratio": "2:3" if portrait else "3:2",
                         "image_size": image_size or get_image_size()},
    )


# --- Rueckseite ---------------------------------------------------------------------------------

def build_back_prompt(req: GenerationRequest, wish: str = "", with_tracklist: bool = True) -> str:
    """Prompt fuer die Rueckseite: Gegenstueck zur (angehaengten) Vorderseite im
    selben Stil, ruhiger komponiert; optional nach Wunsch des Nutzers."""
    from core.card_text import describe_work
    work = describe_work(req.info, req.brief)
    shape = "tall portrait (2:3)" if req.orientation == ORIENTATION_PORTRAIT else "wide landscape (3:2)"
    content = req.brief_text.strip() or req.brief.as_text()
    lines = [
        f"Create the BACK side artwork for a children's RFID music card ({shape} format). "
        "The attached image is the FRONT side of the same card.",
        f"Audio play / series: {work.series or work.album_tag or 'unknown'}"
        + (f" by {work.artist}" if work.artist else ""),
    ]
    if work.episodes:
        lines.append("Episode(s): " + "; ".join(work.episodes))
    if content:
        lines.append(f"Background information (German):\n{content}")
    if wish.strip():
        lines.append(f"The user wants this scene on the back (German, highest priority): {wish.strip()}")
    else:
        hint = "a secondary scene, place or object from the same story"
        if len(work.episodes) > 1:
            hint = f"a scene from the second episode ({work.episodes[1]})"
        lines.append(f"Show {hint} - a companion to the front, not a copy of it.")
    lines.append(f"Style: {STYLE_PRESETS.get(req.style, req.style)}; exactly the same illustration style, "
                 "colour palette and character designs as the attached front side.")
    if req.has_series:
        anchors = len(req.anchors())
        if req.series_bible.strip():
            label = f" for the series \"{req.series_name}\"" if req.series_name else ""
            lines.append(f"Series style bible{label} (German, binding for characters, style and colors):\n"
                         f"{req.series_bible.strip()}")
        if anchors:
            lines.append(f"After the front side, {anchors} further attached image(s) are the series reference "
                         "images (character sheet or earlier covers of the same series): use exactly the same "
                         "characters, illustration style and palette as the reference images.")
    lines += [
        "Composition: calmer and less detailed than the front; "
        + ("keep a large, even, low-contrast area in the upper two thirds where a text panel with the "
           "track list will be placed later; " if with_tracklist else "full-bleed artwork; ")
        + "no frame, no border.",
        "IMPORTANT: absolutely no text, letters, numbers, logos or watermarks in the image.",
        "Child-friendly, cheerful, no scary or violent content.",
    ]
    return "\n\n".join(lines)


def generate_back_art(client, model: str, req: GenerationRequest, front_bytes: bytes, wish: str = "",
                      with_tracklist: bool = True, image_size: str = "") -> Image.Image:
    """Rueckseiten-Motiv vom Bildmodell, mit der Vorderseite als Referenzbild"""
    prompt = build_back_prompt(req, wish, with_tracklist)
    # Vorderseite zuerst, danach die Anker der Serie (zusammen hoechstens MAX_REFERENCE_IMAGES)
    images = [front_bytes] + [a for a in req.anchors() if a != front_bytes][:MAX_REFERENCE_IMAGES - 1]
    return request_image(
        client, CONTEXT_BACK,
        model=model,
        input=[{"type": "text", "text": prompt}] + [
            {"type": "image", "data": base64.b64encode(data).decode("ascii"), "mime_type": _sniff_mime(data)}
            for data in images],
        response_format={"type": "image", "aspect_ratio": "2:3" if req.orientation == ORIENTATION_PORTRAIT else "3:2",
                         "image_size": image_size or get_image_size()},
    )


IDEAS_PROMPT_IMAGE = """Das Bild ist die gewählte VORDERSEITE einer Kinder-Hörspielkarte ({title}).
Beschreibe zuerst in einem Satz, was darauf zu sehen ist (Figuren, Stil, Farben).
Dann nenne drei kurze Bildideen für die RÜCKSEITE derselben Karte: ruhige Nebenszenen, Orte oder
Gegenstände, die zu genau diesen Figuren, diesem Stil und dieser Farbwelt passen, das Hauptmotiv aber
nicht wiederholen; kindgerecht, ohne Text im Bild. Das Bild hat Vorrang; der folgende Hintergrund zum
Inhalt dieses Albums darf die Ideen ergänzen, aber nicht zu anderen Figuren oder Alben führen.
Hintergrund: {content}
Antworte NUR mit JSON: {{"front": "<Beschreibung>", "ideas": ["<Idee 1>", "<Idee 2>", "<Idee 3>"]}}"""

IDEAS_PROMPT_TEXT = """Für die Rückseite einer Kinder-Hörspielkarte suchen wir drei kurze Bildideen:
ruhige Nebenszenen, Orte oder Gegenstände aus derselben Geschichte, kindgerecht, ohne Text im Bild.
Reihe/Werk: {title}. Hintergrund: {content}
Antworte NUR mit JSON: {{"front": "", "ideas": ["<Idee 1>", "<Idee 2>", "<Idee 3>"]}}"""


def suggest_back_ideas(client, text_model: str, req: GenerationRequest,
                       front_bytes: Optional[bytes] = None) -> List[str]:
    """Drei Rueckseiten-Ideen vom Textmodell (guenstiger Aufruf, ohne Suche).
    Mit front_bytes gilt ausschliesslich das Bild der gewaehlten Vorderseite:
    das Modell beschreibt es erst und leitet daraus die Ideen ab. Die
    Beschreibung steht danach in suggest_back_ideas.last_front_description."""
    import json
    import re
    from core.card_text import describe_work
    work = describe_work(req.info, req.brief)
    title = work.title or work.album_tag or "(unbekannt)"
    if front_bytes:
        model_input = [{"type": "image", "data": base64.b64encode(front_bytes).decode("ascii"),
                        "mime_type": _sniff_mime(front_bytes)},
                       {"type": "text", "text": IDEAS_PROMPT_IMAGE.format(
                           title=title, content=(req.brief_text.strip() or req.brief.as_text() or "(keiner)")[:800])}]
    else:
        model_input = IDEAS_PROMPT_TEXT.format(
            title=title, content=(req.brief_text.strip() or req.brief.as_text() or "(keiner)")[:1500])
    interaction = client.interactions.create(model=text_model, input=model_input)
    text = getattr(interaction, "output_text", "") or ""
    match = re.search(r"\{.*\}", text, re.DOTALL)
    suggest_back_ideas.last_front_description = ""
    if match:
        data = json.loads(match.group(0))
        suggest_back_ideas.last_front_description = str(data.get("front", "") or "")
        ideas = data.get("ideas", [])
    else:
        ideas = [line.strip("-• ") for line in text.splitlines() if line.strip()]
    return [str(idea).strip() for idea in ideas if str(idea).strip()][:3]


suggest_back_ideas.last_front_description = ""
