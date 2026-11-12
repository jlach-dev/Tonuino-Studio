"""
Bekannte ESPuino-Karten: welche Karte welchen Inhalt hat, unabhaengig vom Geraet.

Die Zuweisung "Karte -> Ordner" steht im Speicher (NVS) eines ESPuino, die
Dateien auf seiner SD-Karte. Beides kann wechseln: eine neue SD-Karte, ein
zweiter ESPuino. Studio merkt sich deshalb selbst, wofuer eine Karte steht,
in <Datenordner>/espuino/cards.json (liegt mit dem Datenordner oft auf Google
Drive und gilt dann auf allen PCs):

    {"version": 1, "cards": {"<Kartennummer>": {
        "name", "sd_folder" ("/Ordner"), "play_mode", "library_path",
        "album_id" (TONUINO_ALBUM_ID des Bibliotheksalbums), "updated"}}}

Mit album_id bzw. library_path findet Studio das Hoerspiel in der Bibliothek
wieder (fuer Cover und zum erneuten Uebertragen), auch wenn die SD-Karte
gerade nicht im PC steckt.

Eigene Datei des Forks (siehe core/branding.py).
"""

import json
import logging
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Dict, Optional

from core.espuino_transfer import safe_name
from core.fileutil import replace_with_retry

log = logging.getLogger(__name__)

FOLDER = "espuino"
FILENAME = "cards.json"

# Status einer bekannten Karte auf dem aktuellen ESPuino
ASSIGNED = "assigned"  # Zuweisung zeigt auf den bekannten Ordner
ASSIGNED_OTHER = "other"  # Karte ist auf dem Geraet anders belegt
NOT_ASSIGNED = "none"


@dataclass
class KnownCard:
    card_id: str
    name: str = ""
    sd_folder: str = ""
    play_mode: int = 3
    library_path: str = ""
    album_id: str = ""
    updated: str = ""

    @property
    def label(self) -> str:
        return self.name.strip() or self.sd_folder.strip("/").rpartition("/")[2] or f"Karte {self.card_id}"


@dataclass
class KnownCards:
    path: Optional[Path] = None
    cards: Dict[str, KnownCard] = field(default_factory=dict)

    def get(self, card_id: str) -> Optional[KnownCard]:
        return self.cards.get(str(card_id))

    def remember(self, card_id: str, *, name: str = None, sd_folder: str = None, play_mode: int = None,
                 library_path: str = None, album_id: str = None) -> KnownCard:
        """Karte anlegen oder aendern; None laesst ein Feld unveraendert"""
        card_id = str(card_id)
        card = self.cards.get(card_id) or KnownCard(card_id)
        for key, value in (("name", name), ("sd_folder", sd_folder), ("play_mode", play_mode),
                           ("library_path", library_path), ("album_id", album_id)):
            if value is not None:
                setattr(card, key, value)
        card.updated = datetime.now().isoformat(timespec="seconds")
        self.cards[card_id] = card
        return card

    def merge_assignment(self, card_id: str, sd_folder: str, play_mode: int) -> bool:
        """Zuweisung vom Geraet uebernehmen, wenn die Karte noch unbekannt ist.
        Bekannte Karten bleiben, wie sie sind (der Geraetestand kann veraltet sein).
        True, wenn sich etwas geaendert hat."""
        if str(card_id) in self.cards:
            return False
        self.remember(card_id, name=sd_folder.strip("/").rpartition("/")[2], sd_folder=sd_folder,
                      play_mode=play_mode)
        return True

    def save(self) -> None:
        if self.path is None:
            return
        data = {"version": 1, "cards": {cid: {k: v for k, v in asdict(c).items() if k != "card_id"}
                                        for cid, c in sorted(self.cards.items())}}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        replace_with_retry(tmp, self.path)


def cards_file(data_dir: str) -> Path:
    return Path(data_dir) / FOLDER / FILENAME


def load(data_dir: str) -> KnownCards:
    """Kartenverzeichnis aus dem Datenordner; fehlt es, ist es leer. Ohne Datenordner
    (data_dir leer) wird nichts gespeichert."""
    if not data_dir:
        return KnownCards()
    path = cards_file(data_dir)
    known = KnownCards(path=path)
    if not path.is_file():
        return known
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        # Nicht leer weiterarbeiten: der naechste save() wuerde die Datei ueberschreiben
        log.warning("Kartenverzeichnis %s nicht lesbar: %s", path, exc)
        known.path = None
        return known
    for card_id, entry in (data.get("cards") or {}).items():
        if not isinstance(entry, dict) or not str(card_id).isdigit():
            continue
        fields = {k: v for k, v in entry.items() if k in KnownCard.__dataclass_fields__ and k != "card_id"}
        try:
            fields["play_mode"] = int(fields.get("play_mode") or 3)
        except (TypeError, ValueError):
            fields["play_mode"] = 3
        known.cards[str(card_id)] = KnownCard(str(card_id), **fields)
    return known


def find_library_album(card: KnownCard, index):
    """Bibliotheksalbum einer Karte: ueber die Album-ID, den gemerkten Pfad oder
    einen Albumnamen, der zum Ordnernamen auf der SD-Karte passt"""
    if index is None:
        return None
    albums = [a for a in index.albums if not a.offline and a.tracks]
    if card.album_id:
        match = next((a for a in albums if a.album_id == card.album_id), None)
        if match is not None:
            return match
    if card.library_path:
        match = index.find(card.library_path)
        if match is not None and not match.offline and match.tracks:
            return match
    folder = card.sd_folder.strip("/").rpartition("/")[2].casefold()
    if folder:
        for album in albums:
            if folder in (safe_name(album.name).casefold(), safe_name(Path(album.path).name).casefold()):
                return album
    return None
