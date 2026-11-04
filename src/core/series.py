"""
Serienprofile: ein einheitlicher Look fuer alle Folgen einer Hoerspielreihe

  <data_dir>/series/<series_id>/series.json   Name, MusicBrainz-Serie, Stil,
                                              Stilbibel, Anker-Bilder

Die Anker sind Cover aus dem Archiv (core/cover_archive.py), die bei jeder
KI-Erzeugung einer Folge der Reihe als erste Referenzbilder mitgehen -
Figuren, Illustrationsstil und Farbwelt bleiben so ueber alle Karten
gleich. Die Stilbibel beschreibt dasselbe in Worten (Deutsch, editierbar)
und wird an den Bild-Prompt angehaengt.
"""

import base64
import json
import logging
import re
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable, List, Optional, Tuple

from PIL import Image

log = logging.getLogger(__name__)

MAX_ANCHORS = 4

SIDE_FRONT = "front"
SIDE_BACK = "back"


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


@dataclass
class AnchorRef:
    album_id: str
    cover_id: str
    side: str = SIDE_FRONT

    @classmethod
    def from_dict(cls, data: dict) -> "AnchorRef":
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})

    def same(self, other: "AnchorRef") -> bool:
        return (self.album_id, self.cover_id, self.side) == (other.album_id, other.cover_id, other.side)


@dataclass
class SeriesProfile:
    id: str
    name: str = ""
    mb_series_id: str = ""
    style: str = ""  # Stilvorgabe (Schluessel aus STYLE_PRESETS), leer = Vorgabe des Dialogs
    style_bible: str = ""  # Figuren, Palette, Bildsprache (Deutsch)
    anchors: List[AnchorRef] = field(default_factory=list)
    created: str = ""
    updated: str = ""

    @classmethod
    def from_dict(cls, data: dict) -> "SeriesProfile":
        fields = {k: v for k, v in data.items() if k in cls.__dataclass_fields__}
        fields["anchors"] = [AnchorRef.from_dict(a) for a in data.get("anchors", [])]
        return cls(**fields)

    def add_anchor(self, album_id: str, cover_id: str, side: str = SIDE_FRONT) -> bool:
        """False, wenn der Anker schon drin ist oder MAX_ANCHORS erreicht sind"""
        ref = AnchorRef(album_id, cover_id, side)
        if any(a.same(ref) for a in self.anchors) or len(self.anchors) >= MAX_ANCHORS:
            return False
        self.anchors.append(ref)
        return True

    def remove_anchor(self, album_id: str, cover_id: str, side: str = SIDE_FRONT):
        ref = AnchorRef(album_id, cover_id, side)
        self.anchors = [a for a in self.anchors if not a.same(ref)]


class SeriesStore:
    def __init__(self, root: str):
        self.root = Path(root)

    def _dir(self, series_id: str) -> Path:
        if not series_id or any(c in series_id for c in "/\\.:"):
            raise ValueError(f"Ungueltige Serien-ID: {series_id!r}")
        return self.root / "series" / series_id

    def load(self, series_id: str) -> Optional[SeriesProfile]:
        try:
            data = json.loads((self._dir(series_id) / "series.json").read_text(encoding="utf-8"))
            return SeriesProfile.from_dict(data)
        except (OSError, ValueError, TypeError):
            return None

    def save(self, profile: SeriesProfile):
        from core.cover_archive import _write_json_atomic
        if not profile.created:
            profile.created = _now()
        profile.updated = _now()
        _write_json_atomic(self._dir(profile.id) / "series.json", asdict(profile))

    def create(self, name: str, mb_series_id: str = "", style: str = "") -> SeriesProfile:
        profile = SeriesProfile(id=uuid.uuid4().hex[:12], name=name.strip(), mb_series_id=mb_series_id, style=style)
        self.save(profile)
        return profile

    def delete(self, series_id: str):
        import shutil
        shutil.rmtree(self._dir(series_id), ignore_errors=True)

    def list_all(self) -> List[SeriesProfile]:
        base = self.root / "series"
        if not base.is_dir():
            return []
        result = []
        for child in base.iterdir():
            profile = self.load(child.name) if child.is_dir() else None
            if profile:
                result.append(profile)
        return sorted(result, key=lambda p: p.name.lower())

    def find_by_name(self, name: str) -> Optional[SeriesProfile]:
        key = normalize_series_name(name)
        if not key:
            return None
        for profile in self.list_all():
            if normalize_series_name(profile.name) == key:
                return profile
        return None

    def find_by_mb_id(self, mb_series_id: str) -> Optional[SeriesProfile]:
        if not mb_series_id:
            return None
        for profile in self.list_all():
            if profile.mb_series_id == mb_series_id:
                return profile
        return None

    # --- Anker-Bilder -------------------------------------------------------------------------

    def anchor_images(self, profile: SeriesProfile, archive) -> List[bytes]:
        """Rohe Bilddaten der Anker (fehlende werden uebersprungen)"""
        images = []
        for anchor in profile.anchors:
            data = load_anchor_bytes(archive, anchor)
            if data:
                images.append(data)
        return images


def load_anchor_bytes(archive, anchor: AnchorRef) -> Optional[bytes]:
    try:
        entry = next((e for e in archive.covers(anchor.album_id) if e.id == anchor.cover_id), None)
        if entry is None:
            return None
        if anchor.side == SIDE_BACK:
            return archive.load_back_bytes(anchor.album_id, entry)
        return archive.load_bytes(anchor.album_id, entry)
    except (OSError, ValueError):
        return None


# --- Erkennung ---------------------------------------------------------------------------------

_EPISODE_SUFFIX = re.compile(r"\s*[·\-–:]\s*(folge|episode|teil|band|vol\.?|nr\.?)\s*\d+\s*$", re.IGNORECASE)


def normalize_series_name(name: str) -> str:
    """'Die Fuchsbande · Folge 39' -> 'die fuchsbande' (Vergleichsschluessel)"""
    text = _EPISODE_SUFFIX.sub("", (name or "").strip())
    return re.sub(r"\s+", " ", text).strip().lower()


def clean_series_name(name: str) -> str:
    """Anzeigename ohne Folgennummer"""
    return _EPISODE_SUFFIX.sub("", (name or "").strip()).strip()


def detect_series(info, release_group_id: str = "",
                  lookup: Optional[Callable[[str], List[dict]]] = None) -> Tuple[str, str]:
    """(Name, MusicBrainz-Serien-ID) der Reihe, zu der ein Ordner gehoert:
    MusicBrainz-Serienrelation der Release-Group, wenn eine ID bekannt ist,
    sonst die aus den Tags abgeleitete Reihe (core/card_text.py). Leer, wenn
    nichts erkannt wurde. lookup ist austauschbar (Tests, kein Netz)."""
    if release_group_id:
        if lookup is None:
            from core.musicbrainz import release_group_series
            lookup = release_group_series
        try:
            for relation in lookup(release_group_id):
                if relation.get("name"):
                    return relation["name"], relation.get("id", "")
        except Exception:
            log.warning("MusicBrainz-Serie fuer %s nicht abrufbar", release_group_id, exc_info=True)
    from core.card_text import describe_work
    return clean_series_name(describe_work(info).series), ""


# --- Stilbibel (Textmodell) ----------------------------------------------------------------------

STYLE_BIBLE_PROMPT = """Du erstellst eine kurze „Stilbibel“ für die Kartencover einer Kinder-Hörspielreihe: {name}.
Die angehängten Bilder sind die Referenzbilder (Anker) dieser Reihe – sie haben Vorrang vor allem anderen.
Beschreibe daraus auf Deutsch, knapp und konkret, so dass ein Bildmodell weitere Cover im selben Look malen kann:

Figuren: die Hauptfiguren (Name, falls bekannt; Aussehen, Kleidung, typische Merkmale – je ein Satz).
Stil: Illustrationstechnik, Linien, Schattierung, Detailgrad.
Farben: 4-6 Farben der Palette mit kurzer Beschreibung.
Bildsprache: Perspektive, Stimmung, wiederkehrende Elemente.

Hintergrund zur Reihe aus der Recherche (nur ergänzend, nicht wörtlich übernehmen):
{research}

Antworte nur mit der Stilbibel in genau diesen vier Absätzen (Figuren, Stil, Farben, Bildsprache),
insgesamt höchstens 220 Wörter, ohne Einleitung."""


def _sniff_mime(data: bytes) -> str:
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return "image/jpeg"


def _image_parts(images: List[bytes]) -> List[dict]:
    return [{"type": "image", "data": base64.b64encode(data).decode("ascii"), "mime_type": _sniff_mime(data)}
            for data in images]


def suggest_style_bible(client, text_model: str, name: str, anchor_images: List[bytes],
                        research: str = "") -> str:
    """Stilbibel vom Textmodell aus bis zu MAX_ANCHORS Anker-Bildern und der Recherche"""
    prompt = STYLE_BIBLE_PROMPT.format(name=name or "(unbekannt)", research=(research or "(keine)").strip()[:1500])
    images = anchor_images[:MAX_ANCHORS]
    model_input = [{"type": "text", "text": prompt}] + _image_parts(images) if images else prompt
    interaction = client.interactions.create(model=text_model, input=model_input)
    text = (getattr(interaction, "output_text", "") or "").strip()
    if not text:
        raise RuntimeError("Das Modell hat keine Stilbibel geliefert")
    return text


# --- Charakterblatt (Bildmodell) ----------------------------------------------------------------

CHARACTER_SHEET_PROMPT = (
    "Create a character reference sheet for the children's audio play series \"{name}\": the main characters "
    "standing side by side, full body, front view, friendly neutral poses, on a plain, even, light neutral "
    "background with nothing else. Wide landscape (3:2) format.\n\n"
    "Style: {style}.\n\n"
    "{bible}"
    "{refs}"
    "IMPORTANT: absolutely no text, letters, numbers, labels, logos or watermarks in the image.\n\n"
    "Child-friendly, cheerful, no scary or violent content."
)


def build_character_sheet_prompt(name: str, style: str, style_bible: str, anchor_count: int) -> str:
    from core.cover_generator import STYLE_PRESETS
    bible = f"Series style bible (German):\n{style_bible.strip()}\n\n" if style_bible.strip() else ""
    refs = ""
    if anchor_count:
        refs = (f"The {anchor_count} attached reference images show these characters: draw exactly the same "
                "characters, illustration style and palette as in the reference images.\n\n")
    return CHARACTER_SHEET_PROMPT.format(name=name or "unknown", style=STYLE_PRESETS.get(style, style),
                                         bible=bible, refs=refs)


def generate_character_sheet(client, image_model: str, name: str, style: str, style_bible: str,
                             anchor_images: List[bytes], image_size: str = "") -> Image.Image:
    """Ein Bild mit den Hauptfiguren der Reihe (Nano Banana), Anker als Referenz"""
    from core.ai_settings import get_image_size
    from core.cover_generator import CONTEXT_SHEET, request_image
    images = anchor_images[:MAX_ANCHORS]
    prompt = build_character_sheet_prompt(name, style, style_bible, len(images))
    model_input = [{"type": "text", "text": prompt}] + _image_parts(images) if images else prompt
    return request_image(
        client, CONTEXT_SHEET,
        model=image_model, input=model_input,
        response_format={"type": "image", "aspect_ratio": "3:2", "image_size": image_size or get_image_size()},
    )
