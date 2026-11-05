"""
Cover-Archiv im zentralen Datenordner der Bibliothek

  <data_dir>/albums/<album_id>/album.json    Titel, Interpret, MusicBrainz-IDs, Serie
  <data_dir>/albums/<album_id>/covers.json   Liste der Cover mit Herkunft und Rollen
  <data_dir>/albums/<album_id>/covers/<id>.* die Bilddateien

Hier landen alle behaltenen Cover (MusicBrainz, lokal, Datei, KI-Varianten).
Auf die SD-Karte kommt nur das aktive Kartencover (siehe core/card_store.py).
"""

import hashlib
import io
import json
import logging
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import List, Optional

from PIL import Image

from core.fileutil import replace_with_retry

log = logging.getLogger(__name__)

KIND_AI = "ai"
KIND_MUSICBRAINZ = "musicbrainz"
KIND_ONLINE = "online"  # Deezer, iTunes, MusicBrainz, Webseite (Anbieter in CoverEntry.provider)
KIND_LOCAL = "local"  # eingebettet / cover.jpg im Ordner
KIND_FILE = "file"  # vom PC gewaehlt

ROLE_ALBUM_COVER = "album_cover"
ROLE_CARD_COVER = "card_cover"
ROLE_SERIES_ANCHOR = "series_anchor"
# Album-IDs der Karten aus dem ESPuino-Mediahub (core/mediahub_cards.py)
MEDIAHUB_ALBUM_PREFIX = "mediahub"

KIND_LABELS = {
    KIND_AI: "KI",
    KIND_MUSICBRAINZ: "MusicBrainz",
    KIND_ONLINE: "Online",
    KIND_LOCAL: "Lokal",
    KIND_FILE: "Datei",
}


def sha1(data: bytes) -> str:
    return hashlib.sha1(data).hexdigest()


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


BACK_KIND_AI = "ai"  # CoverEntry.back_kind: KI-Rueckseite


class ArchiveReadError(OSError):
    """Eine Archivdatei (album.json, covers.json) existiert, ist aber nicht
    lesbar - z.B. waehrend Google Drive sie synchronisiert. Wer daraufhin neu
    schreiben wuerde, verloere ihren Inhalt; deshalb wird abgebrochen."""


def _write_json_atomic(path: Path, data):
    """Erst in eine temporaere Datei, dann ersetzen - der Datenordner liegt
    oft in einem Sync-Ordner (Google Drive), halbe Dateien waeren fatal."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    replace_with_retry(tmp, path)


def _sniff_extension(data: bytes) -> str:
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return ".png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return ".webp"
    return ".jpg"


@dataclass
class AlbumRecord:
    id: str
    title: str = ""
    artist: str = ""
    mb_release_group_id: str = ""
    mb_release_id: str = ""
    series_id: str = ""
    paths: List[str] = field(default_factory=list)  # bekannte Orte (SD-Ordner, Bibliothek)
    album_cover_sha1: str = ""  # SHA-1 des zuletzt eingebetteten Album-Covers (bereits archiviert)
    fingerprint: str = ""  # Trackzahl + Laengen (core/library.py) - erkennt Kopien ohne ID-Tag
    created: str = ""

    @classmethod
    def from_dict(cls, data: dict) -> "AlbumRecord":
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})


@dataclass
class CoverEntry:
    id: str
    file: str
    kind: str
    source: str = ""  # URL bzw. Beschreibung der Herkunft
    provider: str = ""  # bei Online-Covern: Deezer, iTunes, MusicBrainz, Webseite
    prompt: str = ""
    model: str = ""
    style: str = ""
    roles: List[str] = field(default_factory=list)
    back_file: str = ""  # zugehoerige Rueckseite (optional)
    back_kind: str = ""  # BACK_KIND_AI: per KI erzeugt (darf ersetzt werden), sonst echte bzw. gewaehlte
    width: int = 0
    height: int = 0
    created: str = ""
    data_sha1: str = ""  # SHA-1 der gespeicherten Bilddatei (Duplikaterkennung)

    @classmethod
    def from_dict(cls, data: dict) -> "CoverEntry":
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})

    @property
    def label(self) -> str:
        text = self.provider or KIND_LABELS.get(self.kind, self.kind)
        if self.style:
            text += f" · {self.style}"
        return text


class CoverArchive:
    def __init__(self, root: str):
        self.root = Path(root)

    # --- Alben ----------------------------------------------------------------------

    def _album_dir(self, album_id: str) -> Path:
        if not album_id or any(c in album_id for c in "/\\.:"):
            raise ValueError(f"Ungueltige Album-ID: {album_id!r}")
        return self.root / "albums" / album_id

    def card_dir(self, album_id: str) -> Path:
        """Ordner fuer card.png/card.json eines Albums, das (noch) nicht auf einer
        SD-Karte liegt - der Cover-Manager aus der Bibliothek speichert hier"""
        return self._album_dir(album_id) / "card"

    def load_album(self, album_id: str) -> Optional[AlbumRecord]:
        try:
            return self._read_album(album_id)
        except ArchiveReadError as e:
            log.warning("%s", e)
            return None

    def _read_album(self, album_id: str) -> Optional[AlbumRecord]:
        """None, wenn es album.json nicht gibt; ArchiveReadError, wenn sie
        existiert, aber nicht lesbar ist"""
        path = self._album_dir(album_id) / "album.json"
        if not path.exists():
            return None
        try:
            return AlbumRecord.from_dict(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, ValueError, TypeError, KeyError) as e:
            raise ArchiveReadError(f"Archivdatei nicht lesbar: {path} ({e})") from e

    def save_album(self, record: AlbumRecord):
        if not record.created:
            record.created = _now()
        _write_json_atomic(self._album_dir(record.id) / "album.json", asdict(record))

    def ensure_album(self, album_id: str, title: str = "", artist: str = "", path: str = "") -> AlbumRecord:
        # _read_album statt load_album: eine vorhandene, gerade unlesbare album.json
        # darf nicht durch einen fast leeren Datensatz ersetzt werden
        record = self._read_album(album_id) or AlbumRecord(id=album_id, title=title, artist=artist)
        changed = not (self._album_dir(album_id) / "album.json").exists()
        if title and not record.title:
            record.title, changed = title, True
        if artist and not record.artist:
            record.artist, changed = artist, True
        if path and path not in record.paths:
            record.paths.append(path)
            changed = True
        if changed:
            self.save_album(record)
        return record

    def all_albums(self) -> List[AlbumRecord]:
        albums_dir = self.root / "albums"
        if not albums_dir.is_dir():
            return []
        result = []
        for child in albums_dir.iterdir():
            record = self.load_album(child.name) if child.is_dir() else None
            if record:
                result.append(record)
        return result

    def find_album(self, title: str, artist: str = "") -> Optional[AlbumRecord]:
        """Album mit gleichem Titel (und Interpret, falls bekannt) - Rueckfall,
        wenn die Tracks noch keine Album-ID tragen"""
        if not title.strip():
            return None
        title_key, artist_key = title.strip().lower(), artist.strip().lower()
        for record in self.all_albums():
            if record.id.startswith(MEDIAHUB_ALBUM_PREFIX):
                continue  # Karten aus dem ESPuino-Mediahub sind keine Alben (core/mediahub_cards.py)
            if record.title.strip().lower() == title_key and (
                    not artist_key or not record.artist or record.artist.strip().lower() == artist_key):
                return record
        return None

    # --- Cover ----------------------------------------------------------------------

    def covers(self, album_id: str) -> List[CoverEntry]:
        """Cover des Albums, deren Bilddatei vorhanden ist (zum Anzeigen)"""
        try:
            entries = self._load_entries(album_id)
        except ArchiveReadError as e:
            log.warning("%s", e)
            return []
        return [e for e in entries if self.cover_path(album_id, e).exists()]

    def _load_entries(self, album_id: str) -> List[CoverEntry]:
        """Alle Eintraege aus covers.json, auch solche, deren Bild (noch) fehlt -
        z.B. weil Google Drive die Bilder nach der JSON-Datei synchronisiert.
        Grundlage fuer jedes Aendern: so geht beim Zurueckschreiben nichts
        verloren. ArchiveReadError, wenn die Datei existiert, aber nicht lesbar ist."""
        path = self._album_dir(album_id) / "covers.json"
        if not path.exists():
            return []
        try:
            return [CoverEntry.from_dict(item) for item in json.loads(path.read_text(encoding="utf-8"))]
        except (OSError, ValueError, TypeError, KeyError) as e:
            raise ArchiveReadError(f"Archivdatei nicht lesbar: {path} ({e})") from e

    def _save_covers(self, album_id: str, entries: List[CoverEntry]):
        _write_json_atomic(self._album_dir(album_id) / "covers.json", [asdict(e) for e in entries])

    def cover_path(self, album_id: str, entry: CoverEntry) -> Path:
        return self._album_dir(album_id) / "covers" / entry.file

    def add_cover(self, album_id: str, image, kind: str, **meta) -> CoverEntry:
        """Legt ein Cover ab. image: PIL-Bild (als PNG gespeichert) oder rohe
        Bilddaten (unveraendert, z.B. Original-JPEG von MusicBrainz)."""
        cover_id = uuid.uuid4().hex[:12]
        if isinstance(image, Image.Image):
            buffer = io.BytesIO()
            image.convert("RGB").save(buffer, "PNG")
            data, ext, size = buffer.getvalue(), ".png", image.size
        else:
            data, ext = bytes(image), _sniff_extension(image)
            with Image.open(io.BytesIO(data)) as img:
                size = img.size
        # Duplikate an den Bilddaten erkennen, nicht an der Herkunft: zwei
        # verschiedene Bilder mit gleichem source-Text (z.B. zweimal "Frueheres
        # Album-Cover") muessen beide ins Archiv
        digest = sha1(data)
        existing = self.find_cover_by_sha1(album_id, digest)
        if existing is not None:
            return existing

        entry = CoverEntry(id=cover_id, file=cover_id + ext, kind=kind, width=size[0], height=size[1],
                           created=_now(), data_sha1=digest,
                           **{k: v for k, v in meta.items() if k in CoverEntry.__dataclass_fields__})
        path = self.cover_path(album_id, entry)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        self._save_covers(album_id, self._load_entries(album_id) + [entry])
        return entry

    def find_cover_by_sha1(self, album_id: str, digest: str) -> Optional[CoverEntry]:
        """Cover des Albums mit genau diesen Bilddaten. Eintraege aus aelteren
        Versionen ohne gespeicherten Hash werden aus der Datei nachberechnet."""
        for entry in self.covers(album_id):
            known = entry.data_sha1
            if not known:
                try:
                    known = sha1(self.cover_path(album_id, entry).read_bytes())
                except OSError:
                    continue
            if known == digest:
                return entry
        return None

    def back_path(self, album_id: str, entry: CoverEntry) -> Optional[Path]:
        return self._album_dir(album_id) / "covers" / entry.back_file if entry.back_file else None

    def set_back(self, album_id: str, cover_id: str, data: Optional[bytes], kind: str = "") -> Optional[CoverEntry]:
        """Haengt eine Rueckseite an ein Cover (None entfernt sie); kind: BACK_KIND_AI oder leer"""
        entries = self._load_entries(album_id)
        for entry in entries:
            if entry.id != cover_id:
                continue
            old = self.back_path(album_id, entry)
            if old is not None:
                old.unlink(missing_ok=True)
            if data:
                entry.back_file = f"{entry.id}_back{_sniff_extension(data)}"
                entry.back_kind = kind
                self.back_path(album_id, entry).write_bytes(bytes(data))
            else:
                entry.back_file, entry.back_kind = "", ""
            self._save_covers(album_id, entries)
            return entry
        return None

    def load_back_bytes(self, album_id: str, entry: CoverEntry) -> Optional[bytes]:
        path = self.back_path(album_id, entry)
        try:
            return path.read_bytes() if path is not None else None
        except OSError:
            return None

    def delete_cover(self, album_id: str, cover_id: str):
        entries = self._load_entries(album_id)
        for entry in entries:
            if entry.id == cover_id:
                self.cover_path(album_id, entry).unlink(missing_ok=True)
                back = self.back_path(album_id, entry)
                if back is not None:
                    back.unlink(missing_ok=True)
        self._save_covers(album_id, [e for e in entries if e.id != cover_id])

    def set_role(self, album_id: str, cover_id: str, role: str, exclusive: bool = True):
        """Vergibt eine Rolle; exclusive entzieht sie allen anderen Covern des Albums"""
        entries = self._load_entries(album_id)
        for entry in entries:
            if entry.id == cover_id:
                if role not in entry.roles:
                    entry.roles.append(role)
            elif exclusive and role in entry.roles:
                entry.roles.remove(role)
        self._save_covers(album_id, entries)

    def clear_role(self, album_id: str, cover_id: str, role: str):
        entries = self._load_entries(album_id)
        for entry in entries:
            if entry.id == cover_id and role in entry.roles:
                entry.roles.remove(role)
        self._save_covers(album_id, entries)

    def load_bytes(self, album_id: str, entry: CoverEntry) -> bytes:
        return self.cover_path(album_id, entry).read_bytes()

    # --- Serien (siehe core/series.py) ---------------------------------------------------------

    @property
    def series(self):
        """SeriesStore im selben Datenordner"""
        from core.series import SeriesStore
        return SeriesStore(str(self.root))

    def assign_album(self, album_id: str, series_id: str, title: str = "", artist: str = ""):
        """Album einer Serie zuordnen (leere series_id = keine Serie)"""
        record = self.ensure_album(album_id, title, artist)
        if record.series_id != series_id:
            record.series_id = series_id
            self.save_album(record)

    def albums_in_series(self, series_id: str) -> List[AlbumRecord]:
        return [a for a in self.all_albums() if series_id and a.series_id == series_id]

    def load_image(self, album_id: str, entry: CoverEntry) -> Image.Image:
        with Image.open(self.cover_path(album_id, entry)) as img:
            img.load()
            return img.convert("RGB")
