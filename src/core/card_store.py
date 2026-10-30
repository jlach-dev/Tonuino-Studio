"""
Ablage der Kartencover im jeweiligen Tonuino-Ordner auf der SD-Karte

  NN/card.png          fertige Karte (ID-1 inkl. Beschnitt, mit Titel)
  NN/card_art_raw.png  Motiv ohne Text (fuer erneutes Rendern ohne API-Kosten)
  NN/card.json         Einstellungen und Herkunft (Titel, Ausrichtung, Prompt, Modell)
  NN/card_back.png     Rueckseite (optional), NN/card_back_raw.png ihr Rohbild

Die Ordnernummern bleiben beim Bereinigen unveraendert (siehe SDCard.purge),
daher wandern die Dateien nie und bleiben dem richtigen Ordner zugeordnet.
"""

import io
import json
import os
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional

from PIL import Image

from core.card_layout import (
    BACK_MODE_NONE, BACK_MODE_TRACKLIST, CardText, ORIENTATION_PORTRAIT, TextStyle, render_back_card, render_card,
)

CARD_IMAGE = "card.png"
CARD_RAW = "card_art_raw.png"
CARD_META = "card.json"
CARD_BACK = "card_back.png"
CARD_BACK_RAW = "card_back_raw.png"
CARD_FILES = (CARD_IMAGE, CARD_RAW, CARD_META, CARD_BACK, CARD_BACK_RAW)


@dataclass
class CardMeta:
    title: str = ""  # eine Zeile je Folge
    series: str = ""  # Hoerspielreihe (klein ueber dem Titel)
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
    back_mode: str = BACK_MODE_NONE  # "", "image", "ai", "blur", "gradient" (siehe card_layout)
    back_tracklist: bool = False  # Ueberschrift + Trackliste auf die Rueckseite setzen
    back_prompt: str = ""  # Wunschtext fuer die KI-Rueckseite
    back_heading: str = ""
    back_lines: list = field(default_factory=list)  # Trackliste fuer die Rueckseite
    back_archive_cover_id: str = ""
    back_extended: bool = False  # Rand per KI ergaenzt
    title_style: dict = field(default_factory=dict)  # TextStyle (Schrift, Groesse, Farbe) fuer den Titel
    series_style: dict = field(default_factory=dict)  # ... fuer die Reihe

    @classmethod
    def from_dict(cls, data: dict) -> "CardMeta":
        known = {k: v for k, v in data.items() if k in cls.__dataclass_fields__}
        return cls(**known)


def card_paths(folder_path: str):
    base = Path(folder_path)
    return base / CARD_IMAGE, base / CARD_RAW, base / CARD_META


def has_card(folder_path: str) -> bool:
    return card_paths(folder_path)[0].exists()


def back_paths(folder_path: str):
    base = Path(folder_path)
    return base / CARD_BACK, base / CARD_BACK_RAW


def has_back(folder_path: str) -> bool:
    return back_paths(folder_path)[0].exists()


def load_back_raw(folder_path: str) -> Optional[Image.Image]:
    raw_path = back_paths(folder_path)[1]
    if not raw_path.exists():
        return None
    with Image.open(raw_path) as img:
        img.load()
        return img.copy()


def render_back_from_meta(art: Image.Image, meta: CardMeta, spec=None) -> Image.Image:
    if meta.back_tracklist or meta.back_mode == BACK_MODE_TRACKLIST:  # (alt: eigener Modus)
        return render_back_card(art, meta.orientation, meta.back_heading, meta.back_lines, spec)
    return render_back_card(art, meta.orientation, spec=spec)


def _write_atomic(path: Path, data: bytes):
    """Erst in eine temporaere Datei daneben, dann ersetzen: wird die Karte beim
    Schreiben gezogen, bleibt die alte Datei heil statt einer halben neuen"""
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "wb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)


def _write_png(img: Image.Image, path: Path):
    buffer = io.BytesIO()
    img.save(buffer, "PNG", dpi=(300, 300))
    _write_atomic(path, buffer.getvalue())


def _save_meta(folder_path: str, meta: CardMeta):
    data = json.dumps(asdict(meta), ensure_ascii=False, indent=2).encode("utf-8")
    _write_atomic(card_paths(folder_path)[2], data)


def save_back(folder_path: str, art: Image.Image, meta: CardMeta) -> Image.Image:
    """Rendert die Rueckseite, schreibt card_back.png/_raw.png und aktualisiert card.json"""
    back = render_back_from_meta(art, meta)
    back_path, raw_path = back_paths(folder_path)
    _write_png(art.convert("RGB"), raw_path)
    _write_png(back, back_path)
    _save_meta(folder_path, meta)
    return back


def delete_back(folder_path: str, meta: Optional[CardMeta] = None):
    for path in back_paths(folder_path):
        path.unlink(missing_ok=True)
    if meta is not None:
        meta.back_mode, meta.back_archive_cover_id, meta.back_extended = BACK_MODE_NONE, "", False
        if card_paths(folder_path)[2].exists():
            _save_meta(folder_path, meta)


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
    text = CardText(title=meta.title, folder_number=folder_number if meta.show_number else None,
                    series=meta.series,
                    title_style=TextStyle.from_dict(meta.title_style) if meta.title_style else None,
                    series_style=TextStyle.from_dict(meta.series_style) if meta.series_style else None)
    return render_card(art, text, meta.orientation, spec)


def save_card(folder_path: str, art: Image.Image, meta: CardMeta, folder_number: Optional[int]) -> Image.Image:
    """Rendert die Karte und schreibt alle drei Dateien, jede atomar (siehe
    _write_atomic). card.json zuletzt: sie verweist auf die beiden Bilder."""
    image_path, raw_path, meta_path = card_paths(folder_path)
    if not meta.created:
        meta.created = datetime.now().isoformat(timespec="seconds")
    card = render_from_meta(art, meta, folder_number)

    _write_png(art.convert("RGB"), raw_path)
    _write_png(card, image_path)
    _save_meta(folder_path, meta)
    return card


def delete_card(folder_path: str):
    for path in card_paths(folder_path) + back_paths(folder_path):
        path.unlink(missing_ok=True)
