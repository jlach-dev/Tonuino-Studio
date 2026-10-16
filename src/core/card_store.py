"""
Ablage der Kartencover im jeweiligen Tonuino-Ordner auf der SD-Karte

  NN/card.png          fertige Karte (ID-1 inkl. Beschnitt, mit Titel)
  NN/card_art_raw.png  Motiv ohne Text (fuer erneutes Rendern ohne API-Kosten)
  NN/card.json         Einstellungen und Herkunft (Titel, Ausrichtung, Prompt, Modell)

Die Ordnernummern bleiben beim Bereinigen unveraendert (siehe SDCard.purge),
daher wandern die Dateien nie und bleiben dem richtigen Ordner zugeordnet.
"""

import io
import json
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional

from PIL import Image

from core.card_layout import CardText, ORIENTATION_PORTRAIT, render_card

CARD_IMAGE = "card.png"
CARD_RAW = "card_art_raw.png"
CARD_META = "card.json"
CARD_FILES = (CARD_IMAGE, CARD_RAW, CARD_META)


@dataclass
class CardMeta:
    title: str = ""
    orientation: str = ORIENTATION_PORTRAIT
    show_number: bool = True
    source: str = ""  # "ai", "blur"
    style: str = ""
    prompt: str = ""
    model: str = ""
    research_sources: list = field(default_factory=list)
    created: str = ""
    album_id: str = ""  # Verknuepfung zum Cover-Archiv bzw. zur Bibliothek
    archive_cover_id: str = ""

    @classmethod
    def from_dict(cls, data: dict) -> "CardMeta":
        known = {k: v for k, v in data.items() if k in cls.__dataclass_fields__}
        return cls(**known)


def card_paths(folder_path: str):
    base = Path(folder_path)
    return base / CARD_IMAGE, base / CARD_RAW, base / CARD_META


def has_card(folder_path: str) -> bool:
    return card_paths(folder_path)[0].exists()


def load_meta(folder_path: str) -> Optional[CardMeta]:
    meta_path = card_paths(folder_path)[2]
    try:
        return CardMeta.from_dict(json.loads(meta_path.read_text(encoding="utf-8")))
    except (OSError, ValueError, TypeError):
        return None


def load_raw_art(folder_path: str) -> Optional[Image.Image]:
    raw_path = card_paths(folder_path)[1]
    if not raw_path.exists():
        return None
    with Image.open(raw_path) as img:
        img.load()
        return img.copy()


def render_from_meta(art: Image.Image, meta: CardMeta, folder_number: Optional[int], spec=None) -> Image.Image:
    text = CardText(title=meta.title, folder_number=folder_number if meta.show_number else None)
    return render_card(art, text, meta.orientation, spec)


def save_card(folder_path: str, art: Image.Image, meta: CardMeta, folder_number: Optional[int]) -> Image.Image:
    """Rendert die Karte und schreibt alle drei Dateien. Erst in einen Puffer,
    dann auf die Karte - so bleibt bei einem Fehler keine halbe Datei liegen."""
    image_path, raw_path, meta_path = card_paths(folder_path)
    if not meta.created:
        meta.created = datetime.now().isoformat(timespec="seconds")
    card = render_from_meta(art, meta, folder_number)

    for img, path in ((art.convert("RGB"), raw_path), (card, image_path)):
        buffer = io.BytesIO()
        img.save(buffer, "PNG", dpi=(300, 300))
        path.write_bytes(buffer.getvalue())
    meta_path.write_text(json.dumps(asdict(meta), ensure_ascii=False, indent=2), encoding="utf-8")
    return card


def delete_card(folder_path: str):
    for path in card_paths(folder_path):
        path.unlink(missing_ok=True)
