"""
Bibliothek: die Hoerspiele und Musik des Nutzers auf PC und NAS

Die Musik bleibt, wo sie ist. Die App liest mehrere Quellordner (QSettings
"library/sources") ein und merkt sich das Ergebnis in
<data_dir>/library_index.json, damit die Bibliothek auch dann angezeigt
wird, wenn ein NAS gerade nicht erreichbar ist (Alben dort gelten als
"offline"). Ein Album ist ein Ordner mit Audiodateien; Unterordner wie
"CD1"/"CD2" werden zu einem Album zusammengefasst.

Wiedererkannt werden Alben ueber die App-Album-ID (Tag TONUINO_ALBUM_ID,
siehe core/album_identity.py) - auch nach Umbenennen oder Verschieben. In
die Bibliotheksdateien wird die ID erst geschrieben, wenn der Nutzer das
einmalig erlaubt hat (QSettings "library/write_id_tags"); WAV wird dabei
uebersprungen.
"""

import base64
import hashlib
import io
import json
import logging
import os
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

from core.tag_save import save_audio

log = logging.getLogger(__name__)

AUDIO_EXTENSIONS = (".mp3", ".flac", ".m4a", ".ogg", ".wav", ".opus")
INDEX_FILE = "library_index.json"
THUMBS_DIR = "library_thumbs"
THUMB_SIZE = 240

SETTINGS_SOURCES = "library/sources"
SETTINGS_WRITE_ID_TAGS = "library/write_id_tags"  # "yes" / "no" / "" (noch nicht gefragt)

# Zustand eines Bibliotheksalbums gegenueber dem Cover-Archiv
STATE_LINKED = "linked"  # Album-ID bekannt, Pfad im Archiv eingetragen
STATE_MOVED = "moved"  # Album-ID bekannt, aber unter neuem Pfad (Archiv wurde aktualisiert)
STATE_NEW = "new"  # noch nicht im Archiv
STATE_ORPHANED = "orphaned"  # Archiv-Eintrag, dessen Bibliotheksordner verschwunden ist
STATE_OFFLINE = "offline"  # Quelle (z.B. NAS) gerade nicht erreichbar

STATE_LABELS = {
    STATE_LINKED: "verknüpft",
    STATE_MOVED: "verschoben",
    STATE_NEW: "neu",
    STATE_ORPHANED: "verwaist",
    STATE_OFFLINE: "offline",
}

_DISC_FOLDER = re.compile(r"^(cd|disc|disk|teil|part)\s*[-_ ]?\s*(\d+)$", re.IGNORECASE)
_NUMBER = re.compile(r"(\d+)")

ALBUM_ID_KEY = "TONUINO_ALBUM_ID"
MB_ALBUM_ID_KEY = "MusicBrainz Album Id"
MB_RELEASE_GROUP_KEY = "MusicBrainz Release Group Id"


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _natural_key(name: str):
    return [int(part) if part.isdigit() else part.lower() for part in _NUMBER.split(name)]


def norm_path(path: str) -> str:
    """Vergleichbare Schreibweise eines Pfads (Windows: Gross-/Kleinschreibung egal)"""
    text = os.path.normpath(str(path)).replace("\\", "/")
    return text.lower() if os.name == "nt" else text


def is_audio_file(name: str) -> bool:
    return name.lower().endswith(AUDIO_EXTENSIONS) and not name.startswith("._")


# --- Datenmodell -------------------------------------------------------------------------------

@dataclass
class LibraryAlbum:
    path: str
    source: str = ""  # Quellordner, zu dem das Album gehoert
    title: str = ""
    artist: str = ""
    tracks: List[str] = field(default_factory=list)  # Audiodateien in Abspielreihenfolge
    duration: float = 0.0  # Sekunden, alle Tracks
    album_id: str = ""  # TONUINO_ALBUM_ID aus den Tags
    mb_release_id: str = ""
    mb_release_group_id: str = ""
    fingerprint: str = ""  # Trackzahl + gerundete Laengen (erkennt Kopien ohne ID-Tag)
    thumb: str = ""  # Dateiname des Vorschaubilds in <data_dir>/library_thumbs
    has_cover: bool = False
    signature: str = ""  # Dateianzahl + juengste Aenderung (fuer das inkrementelle Einlesen)
    state: str = STATE_NEW
    offline: bool = False
    scanned: str = ""

    @property
    def track_count(self) -> int:
        return len(self.tracks)

    @property
    def name(self) -> str:
        return self.title or Path(self.path).name

    @classmethod
    def from_dict(cls, data: dict) -> "LibraryAlbum":
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})


@dataclass
class LibraryIndex:
    albums: List[LibraryAlbum] = field(default_factory=list)
    scanned: str = ""
    orphaned: List[dict] = field(default_factory=list)  # Archiv-Eintraege ohne Bibliotheksordner (id, title, artist)
    unreadable: List[str] = field(default_factory=list)  # Ordner, die beim letzten Einlesen nicht lesbar waren

    def by_path(self) -> Dict[str, LibraryAlbum]:
        return {norm_path(a.path): a for a in self.albums}

    def by_id(self) -> Dict[str, LibraryAlbum]:
        return {a.album_id: a for a in self.albums if a.album_id}

    def find(self, path: str) -> Optional[LibraryAlbum]:
        return self.by_path().get(norm_path(path))

    def to_dict(self) -> dict:
        return {"version": 1, "scanned": self.scanned, "orphaned": self.orphaned,
                "albums": [asdict(a) for a in self.albums]}

    @classmethod
    def from_dict(cls, data: dict) -> "LibraryIndex":
        return cls(albums=[LibraryAlbum.from_dict(a) for a in data.get("albums", [])],
                   scanned=data.get("scanned", ""), orphaned=list(data.get("orphaned", [])))


# --- Tags lesen (alle Formate ueber mutagen.File) ------------------------------------------------

@dataclass
class TrackTags:
    title: str = ""
    artist: str = ""
    album: str = ""
    album_artist: str = ""
    duration: float = 0.0
    cover: Optional[bytes] = None
    album_id: str = ""
    mb_release_id: str = ""
    mb_release_group_id: str = ""
    track_number: int = 0
    disc_number: int = 0  # CD-Nummer bei Mehrfach-CDs (0 = unbekannt)
    disc_total: int = 0  # Anzahl CDs, falls im Tag angegeben


def _number_pair(value) -> Tuple[int, int]:
    """'2', '2/3', [(2, 3)] -> (2, 3); fehlende Werte als 0"""
    if isinstance(value, (list, tuple)) and value and isinstance(value[0], tuple):
        pair = value[0]
        return int(pair[0] or 0), int(pair[1] or 0) if len(pair) > 1 else 0
    match = re.match(r"\s*(\d+)(?:\s*/\s*(\d+))?", _first(value))
    if not match:
        return 0, 0
    return int(match.group(1)), int(match.group(2) or 0)


def _track_number(value) -> int:
    """'3', '3/12', (3, 12) -> 3"""
    if isinstance(value, (list, tuple)) and value and isinstance(value[0], tuple):
        value = value[0][0]
    text = _first(value)
    match = re.match(r"\s*(\d+)", text)
    return int(match.group(1)) if match else 0


def _first(value) -> str:
    if value is None:
        return ""
    if isinstance(value, (list, tuple)):
        value = value[0] if value else ""
    if isinstance(value, bytes):
        value = value.decode("utf-8", "replace")
    return str(value).strip()


def _open_any(path: str):
    import mutagen
    try:
        return mutagen.File(path)
    except Exception as e:
        log.debug("Datei nicht lesbar (%s): %s", path, e)
        return None


def read_track_tags(path: str) -> TrackTags:
    """Titel, Interpret, Album, Dauer, Cover und IDs - MP3, FLAC, M4A, OGG/Opus, WAV"""
    tags = TrackTags()
    audio = _open_any(path)
    if audio is None:
        return tags
    tags.duration = float(getattr(audio.info, "length", 0.0) or 0.0)
    raw = audio.tags
    if raw is None:
        return tags
    try:
        from mutagen.id3 import ID3
        from mutagen.mp4 import MP4Tags
    except ImportError:  # pragma: no cover
        return tags

    if isinstance(raw, ID3):
        def frame(key):
            item = raw.get(key)
            return _first(item.text) if item is not None and getattr(item, "text", None) else ""

        def txxx(desc):
            return frame(f"TXXX:{desc}")

        tags.title, tags.artist = frame("TIT2"), frame("TPE1")
        tags.album, tags.album_artist = frame("TALB"), frame("TPE2")
        tags.track_number = _track_number(frame("TRCK"))
        tags.disc_number, tags.disc_total = _number_pair(frame("TPOS"))
        tags.album_id = txxx(ALBUM_ID_KEY)
        tags.mb_release_id, tags.mb_release_group_id = txxx(MB_ALBUM_ID_KEY), txxx(MB_RELEASE_GROUP_KEY)
        for key in raw.keys():
            if key.startswith("APIC"):
                tags.cover = raw[key].data
                break
    elif isinstance(raw, MP4Tags):
        def atom(key):
            return _first(raw.get(key))

        tags.title, tags.artist = atom("\xa9nam"), atom("\xa9ART")
        tags.album, tags.album_artist = atom("\xa9alb"), atom("aART")
        tags.track_number = _track_number(raw.get("trkn"))
        tags.disc_number, tags.disc_total = _number_pair(raw.get("disk"))
        tags.album_id = atom(f"----:com.apple.iTunes:{ALBUM_ID_KEY}")
        tags.mb_release_id = atom(f"----:com.apple.iTunes:{MB_ALBUM_ID_KEY}")
        tags.mb_release_group_id = atom(f"----:com.apple.iTunes:{MB_RELEASE_GROUP_KEY}")
        covers = raw.get("covr")
        if covers:
            tags.cover = bytes(covers[0])
    else:  # Vorbis-Kommentare (FLAC, OGG, Opus)
        def comment(key):
            return _first(raw.get(key) or raw.get(key.lower()) or raw.get(key.upper()))

        tags.title, tags.artist = comment("title"), comment("artist")
        tags.album, tags.album_artist = comment("album"), comment("albumartist")
        tags.track_number = _track_number(comment("tracknumber"))
        tags.disc_number, tags.disc_total = _number_pair(comment("discnumber"))
        if not tags.disc_total:
            tags.disc_total = _number_pair(comment("disctotal") or comment("totaldiscs"))[0]
        tags.album_id = comment(ALBUM_ID_KEY)
        tags.mb_release_id = comment("musicbrainz_albumid")
        tags.mb_release_group_id = comment("musicbrainz_releasegroupid")
        pictures = getattr(audio, "pictures", None)
        if pictures:
            tags.cover = pictures[0].data
        else:
            encoded = raw.get("metadata_block_picture")
            if encoded:
                try:
                    from mutagen.flac import Picture
                    tags.cover = Picture(base64.b64decode(_first(encoded))).data
                except Exception:
                    pass
    return tags


@dataclass
class LibraryTrack:
    """Ein Track eines Bibliotheksalbums (fuer das Album-Panel und den Teilimport)"""
    path: str
    number: int = 0
    title: str = ""
    artist: str = ""
    duration: float = 0.0

    @property
    def display(self) -> str:
        name = self.title or os.path.splitext(os.path.basename(self.path))[0]
        return f"{self.number:02d}  {name}" if self.number else name


def read_album_tracks(album_or_path, cancelled: Callable[[], bool] = lambda: False) -> List[LibraryTrack]:
    """Tracks eines Albums mit Nummer, Titel, Interpret und Dauer - sortiert nach
    Tracknummer, sonst nach Dateiname"""
    if isinstance(album_or_path, (list, tuple)):
        files = list(album_or_path)
    else:
        path = album_or_path if isinstance(album_or_path, str) else album_or_path.path
        files = (album_or_path.tracks if not isinstance(album_or_path, str) and album_or_path.tracks
                 else album_track_files(path))
    tracks = []
    for index, file in enumerate(files, start=1):
        if cancelled():
            break
        tags = read_track_tags(file)
        tracks.append(LibraryTrack(file, tags.track_number or 0, tags.title, tags.artist, tags.duration))
    if any(t.number for t in tracks):
        tracks.sort(key=lambda t: (t.number or 999, _natural_key(os.path.basename(t.path))))
    for index, track in enumerate(tracks, start=1):
        if not track.number:
            track.number = index
    return tracks


# Standardfelder -> (ID3-Frame, MP4-Atom, Vorbis-Kommentar)
_FIELD_MAP = {
    "title": ("TIT2", "\xa9nam", "title"),
    "artist": ("TPE1", "\xa9ART", "artist"),
    "album": ("TALB", "\xa9alb", "album"),
    "album_artist": ("TPE2", "aART", "albumartist"),
    "year": ("TDRC", "\xa9day", "date"),
    "genre": ("TCON", "\xa9gen", "genre"),
}


def _vorbis_key(desc: str) -> str:
    """Picard-Schreibweise der freien Felder in Vorbis-Kommentaren:
    "MusicBrainz Album Id" -> "musicbrainz_albumid"; alles andere unveraendert"""
    if desc.lower().startswith("musicbrainz "):
        return "musicbrainz_" + desc[len("musicbrainz "):].replace(" ", "").lower()
    return desc


def write_tags_any(path: str, fields: Dict[str, object], custom: Optional[Dict[str, str]] = None) -> bool:
    """Schreibt Standardfelder (title, artist, album, album_artist, year, genre,
    track_number, total_tracks) und freie Textfelder (custom: z.B. MusicBrainz-IDs,
    Album-ID) in MP3, FLAC, M4A oder OGG/Opus. WAV wird bewusst nicht angefasst.
    Leerer Wert entfernt das Feld. True, wenn geschrieben wurde."""
    if path.lower().endswith(".wav"):
        return False
    audio = _open_any(path)
    if audio is None:
        return False
    try:
        from mutagen.id3 import ID3, TXXX, TRCK
        import mutagen.id3 as id3
        from mutagen.mp4 import MP4Tags
        if audio.tags is None:
            audio.add_tags()
        raw = audio.tags
        track = fields.get("track_number")
        total = fields.get("total_tracks") or 0
        if isinstance(raw, ID3):
            for name, (frame_name, _atom, _comment) in _FIELD_MAP.items():
                if name not in fields:
                    continue
                value = str(fields[name] or "")
                if value:
                    raw.setall(frame_name, [getattr(id3, frame_name)(encoding=3, text=[value])])
                else:
                    raw.delall(frame_name)
            if track is not None:
                if track:
                    raw.setall("TRCK", [TRCK(encoding=3, text=[f"{track}/{total}" if total else str(track)])])
                else:
                    raw.delall("TRCK")
            for desc, value in (custom or {}).items():
                raw.setall(f"TXXX:{desc}", [TXXX(encoding=3, desc=desc, text=[value])] if value else [])
        elif isinstance(raw, MP4Tags):
            for name, (_frame, atom, _comment) in _FIELD_MAP.items():
                if name not in fields:
                    continue
                value = str(fields[name] or "")
                if value:
                    raw[atom] = [value]
                elif atom in raw:
                    del raw[atom]
            if track is not None:
                if track:
                    raw["trkn"] = [(int(track), int(total))]
                elif "trkn" in raw:
                    del raw["trkn"]
            for desc, value in (custom or {}).items():
                key = f"----:com.apple.iTunes:{desc}"
                if value:
                    raw[key] = [value.encode("utf-8")]
                elif key in raw:
                    del raw[key]
        else:  # Vorbis-Kommentare
            for name, (_frame, _atom, comment) in _FIELD_MAP.items():
                if name not in fields:
                    continue
                value = str(fields[name] or "")
                if value:
                    raw[comment] = [value]
                elif comment in raw:
                    del raw[comment]
            if track is not None:
                if track:
                    raw["tracknumber"] = [str(track)]
                    if total:
                        raw["tracktotal"] = [str(total)]
                elif "tracknumber" in raw:
                    del raw["tracknumber"]
            for desc, value in (custom or {}).items():
                key = _vorbis_key(desc)
                if value:
                    raw[key] = [value]
                elif key in raw:
                    del raw[key]
        save_audio(audio)
        return True
    except Exception as e:
        log.warning("Tags konnten nicht geschrieben werden (%s): %s", path, e)
        return False


def write_cover_any(path: str, data: bytes, mime: str = "image/jpeg") -> bool:
    """Bettet ein Cover in MP3 (APIC), FLAC (Picture), M4A (covr) oder OGG/Opus
    (metadata_block_picture) ein; vorhandene Cover werden ersetzt. WAV wird
    bewusst nicht angefasst. True, wenn geschrieben wurde."""
    if path.lower().endswith(".wav"):
        return False
    audio = _open_any(path)
    if audio is None:
        return False
    try:
        from mutagen.flac import FLAC, Picture
        from mutagen.id3 import APIC, ID3
        from mutagen.mp4 import MP4Cover, MP4Tags
        if audio.tags is None:
            audio.add_tags()
        raw = audio.tags
        if isinstance(raw, ID3):
            # Nur das Front-Cover (Typ 3) ersetzen - Rueckseite, Booklet usw. bleiben
            for key in [k for k, frame in raw.items() if k.startswith("APIC") and frame.type == 3]:
                del raw[key]
            raw.add(APIC(encoding=3, mime=mime, type=3, desc="Cover", data=data))
        elif isinstance(raw, MP4Tags):
            fmt = MP4Cover.FORMAT_PNG if mime == "image/png" else MP4Cover.FORMAT_JPEG
            raw["covr"] = [MP4Cover(data, imageformat=fmt)]
        else:
            picture = Picture()
            picture.type, picture.mime, picture.desc, picture.data = 3, mime, "Cover", data
            if isinstance(audio, FLAC):
                others = [p for p in audio.pictures if p.type != 3]  # nur das Front-Cover ersetzen
                audio.clear_pictures()
                for other in others:
                    audio.add_picture(other)
                audio.add_picture(picture)
            else:  # OGG/Opus: Picture base64-kodiert als Vorbis-Kommentar
                raw["metadata_block_picture"] = [base64.b64encode(picture.write()).decode("ascii")]
        save_audio(audio)
        return True
    except Exception as e:
        log.warning("Cover konnte nicht geschrieben werden (%s): %s", path, e)
        return False


COVER_FILE = "cover.jpg"


def write_album_cover(paths: List[str], folder: str, jpeg: bytes, write_file: bool = True) -> int:
    """Album-Cover (JPEG) in alle Dateien einbetten und als cover.jpg in den
    Albumordner legen; Rueckgabe: Zahl der Dateien mit eingebettetem Cover"""
    written = sum(1 for p in paths if write_cover_any(p, jpeg, "image/jpeg"))
    if write_file and folder:
        try:
            Path(folder, COVER_FILE).write_bytes(jpeg)
        except OSError as e:
            log.warning("cover.jpg konnte nicht geschrieben werden (%s): %s", folder, e)
    return written


def write_album_id_any(path: str, album_id: str) -> bool:
    """Schreibt die App-Album-ID in eine Bibliotheksdatei (MP3, FLAC, M4A, OGG/Opus);
    WAV wird bewusst nicht angefasst. True, wenn geschrieben wurde."""
    return write_tags_any(path, {}, {ALBUM_ID_KEY: album_id})


def write_album_id_to_album(album: "LibraryAlbum", album_id: str) -> int:
    written = sum(1 for path in album.tracks if write_album_id_any(path, album_id))
    if written:
        album.album_id = album_id
    return written


# --- Alben finden -----------------------------------------------------------------------------

def _audio_files(directory: str) -> List[str]:
    try:
        names = [e.name for e in os.scandir(directory) if e.is_file() and is_audio_file(e.name)]
    except OSError:
        return []
    return [os.path.join(directory, n) for n in sorted(names, key=_natural_key)]


def _disc_folders(directory: str, subdirs: List[str]) -> List[str]:
    """Unterordner CD1/CD2/..., wenn alle Unterordner so heissen und Audio enthalten"""
    if not subdirs:
        return []
    discs = []
    for name in subdirs:
        match = _DISC_FOLDER.match(name)
        if not match or not _audio_files(os.path.join(directory, name)):
            return []
        discs.append((int(match.group(2)), name))
    return [name for _, name in sorted(discs)]


def album_track_files(album_path: str) -> List[str]:
    """Audiodateien eines Albumordners (inkl. zusammengefasster CD-Unterordner)"""
    files = _audio_files(album_path)
    if files:
        return files
    try:
        subdirs = [e.name for e in os.scandir(album_path) if e.is_dir() and not e.name.startswith(".")]
    except OSError:
        return []
    for name in _disc_folders(album_path, subdirs):
        files.extend(_audio_files(os.path.join(album_path, name)))
    return files


def find_album_folders(source: str, failed: Optional[List[str]] = None) -> List[Tuple[str, List[str]]]:
    """(Albumpfad, Audiodateien) fuer alle Alben unter einem Quellordner.
    Ordner, die sich nicht lesen liessen (NAS-Aussetzer, fehlende Rechte),
    landen in failed - ihre Alben sind dann nicht weg, nur gerade nicht lesbar."""
    result = []

    def remember(error: OSError):
        if failed is not None:
            failed.append(error.filename or source)

    for dirpath, dirnames, filenames in os.walk(source, onerror=remember):
        dirnames[:] = sorted(d for d in dirnames if not d.startswith(".") and d.lower() != "@eadir")
        files = [os.path.join(dirpath, n) for n in sorted(filenames, key=_natural_key) if is_audio_file(n)]
        if files:
            result.append((dirpath, files))
            continue
        discs = _disc_folders(dirpath, dirnames)
        if discs:
            merged = []
            for name in discs:
                merged.extend(_audio_files(os.path.join(dirpath, name)))
            result.append((dirpath, merged))
            dirnames[:] = []  # CD-Ordner nicht noch einmal als eigene Alben
    return result


def folder_signature(files: List[str]) -> str:
    newest = 0.0
    for path in files:
        try:
            newest = max(newest, os.stat(path).st_mtime)
        except OSError:
            pass
    return f"{len(files)}:{newest:.0f}"


# Zwei verschiedene Singles gleicher Laenge haetten denselben Fingerabdruck
MIN_FINGERPRINT_TRACKS = 2


def fingerprint(durations: List[float]) -> str:
    """Trackzahl + auf ganze Sekunden gerundete Laengen - erkennt dieselbe
    Aufnahme auch als konvertierte Kopie (z.B. auf der SD-Karte)"""
    text = f"{len(durations)}:" + ",".join(str(int(round(d))) for d in durations)
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:16]


def is_offline(source: str) -> bool:
    try:
        return not os.path.isdir(source)
    except OSError:
        return True


def _thumb_name(album_path: str) -> str:
    return hashlib.sha1(norm_path(album_path).encode("utf-8")).hexdigest()[:16] + ".jpg"


def _folder_cover(album_path: str) -> Optional[bytes]:
    for name in ("cover.jpg", "cover.png", "folder.jpg", "folder.png", "front.jpg"):
        candidate = Path(album_path) / name
        if candidate.is_file():
            try:
                return candidate.read_bytes()
            except OSError:
                return None
    return None


def read_album(album_path: str, source: str = "", files: Optional[List[str]] = None,
               thumbs_dir: Optional[Path] = None, max_tag_reads: int = 500) -> LibraryAlbum:
    """Liest ein Album komplett ein (Tags aller Tracks, Cover des ersten Tracks)"""
    files = album_track_files(album_path) if files is None else list(files)
    album = LibraryAlbum(path=str(album_path), source=source, tracks=files, scanned=_now())
    durations: List[float] = []
    cover = None
    for i, path in enumerate(files):
        if i >= max_tag_reads:
            break
        tags = read_track_tags(path)
        durations.append(tags.duration)
        album.title = album.title or tags.album
        album.artist = album.artist or tags.album_artist or tags.artist
        album.album_id = album.album_id or tags.album_id
        album.mb_release_id = album.mb_release_id or tags.mb_release_id
        album.mb_release_group_id = album.mb_release_group_id or tags.mb_release_group_id
        if cover is None and tags.cover:
            cover = tags.cover
    album.duration = sum(durations)
    album.fingerprint = fingerprint(durations)
    album.signature = folder_signature(files)
    if cover is None:
        cover = _folder_cover(album_path)
    album.has_cover = cover is not None
    if cover is not None and thumbs_dir is not None:
        album.thumb = save_thumbnail(cover, thumbs_dir / _thumb_name(album_path))
    return album


def save_thumbnail(data: bytes, target: Path) -> str:
    """Kleines JPEG fuer die Kachelansicht; leerer String, wenn das Bild nicht lesbar ist"""
    try:
        from PIL import Image
        with Image.open(io.BytesIO(data)) as img:
            img = img.convert("RGB")
            img.thumbnail((THUMB_SIZE, THUMB_SIZE))
            target.parent.mkdir(parents=True, exist_ok=True)
            img.save(target, "JPEG", quality=85)
        return target.name
    except Exception as e:
        log.debug("Vorschaubild nicht erzeugt (%s): %s", target, e)
        return ""


# --- Bibliothek -------------------------------------------------------------------------------

class Library:
    """Einlesen, Zwischenspeichern und Abgleich der Bibliothek"""

    def __init__(self, data_dir: str, sources: List[str]):
        self.data_dir = Path(data_dir)
        self.sources = [s for s in sources if s]

    @property
    def index_path(self) -> Path:
        return self.data_dir / INDEX_FILE

    @property
    def thumbs_dir(self) -> Path:
        return self.data_dir / THUMBS_DIR

    def thumb_path(self, album: LibraryAlbum) -> Optional[Path]:
        if not album.thumb:
            return None
        path = self.thumbs_dir / album.thumb
        return path if path.is_file() else None

    # --- Cache ---

    def load(self) -> LibraryIndex:
        try:
            return LibraryIndex.from_dict(json.loads(self.index_path.read_text(encoding="utf-8")))
        except (OSError, ValueError, TypeError):
            return LibraryIndex()

    def save(self, index: LibraryIndex):
        from core.cover_archive import _write_json_atomic
        _write_json_atomic(self.index_path, index.to_dict())

    # --- Scan ---

    def scan(self, previous: Optional[LibraryIndex] = None,
             progress: Optional[Callable[[int, int, str], None]] = None,
             is_cancelled: Optional[Callable[[], bool]] = None) -> LibraryIndex:
        """Liest alle Quellen ein. Unveraenderte Alben (gleiche Signatur) werden
        aus dem letzten Index uebernommen; Alben nicht erreichbarer Quellen
        bleiben als offline erhalten."""
        previous = previous or LibraryIndex()
        known = previous.by_path()
        index = LibraryIndex(scanned=_now(), orphaned=list(previous.orphaned))
        offline_sources = [s for s in self.sources if is_offline(s)]
        found: List[Tuple[str, str, List[str]]] = []
        failed_dirs: List[str] = []
        for source in self.sources:
            if source in offline_sources:
                continue
            for album_path, files in find_album_folders(source, failed_dirs):
                found.append((source, album_path, files))

        total = len(found)
        for i, (source, album_path, files) in enumerate(found, start=1):
            if is_cancelled is not None and is_cancelled():
                break
            if progress is not None:
                progress(i, total, Path(album_path).name)
            cached = known.get(norm_path(album_path))
            if cached is not None and cached.signature == folder_signature(files) and cached.tracks:
                cached.offline, cached.source = False, source
                index.albums.append(cached)
                continue
            album = read_album(album_path, source, files, self.thumbs_dir)
            if cached is not None and not album.has_cover:
                album.thumb = cached.thumb
            index.albums.append(album)

        for source in offline_sources:
            for album in previous.albums:
                if norm_path(album.source) == norm_path(source) or norm_path(album.path).startswith(
                        norm_path(source) + "/"):
                    album.offline = True
                    album.state = STATE_OFFLINE
                    index.albums.append(album)
        # Teilbaeume, die sich gerade nicht lesen liessen (z.B. NAS-Aussetzer mitten
        # im Einlesen): ihre bisherigen Alben bleiben als offline erhalten, statt
        # aus dem Index zu verschwinden und im Archiv als verwaist zu gelten
        failed = [norm_path(d) for d in failed_dirs]
        present = {norm_path(a.path) for a in index.albums}
        for album in previous.albums:
            path = norm_path(album.path)
            if path not in present and any(path == d or path.startswith(d + "/") for d in failed):
                album.offline = True
                album.state = STATE_OFFLINE
                index.albums.append(album)
                present.add(path)
        index.unreadable = list(failed_dirs)
        index.albums.sort(key=lambda a: (a.artist.lower(), a.name.lower()))
        return index

    def refresh_album(self, album_path: str, index: LibraryIndex, archive=None) -> LibraryIndex:
        """Ein einzelnes Album neu einlesen (z.B. nach dem Nachtragen von Tags),
        im Index ersetzen, abgleichen und den Index speichern"""
        key = norm_path(album_path)
        old = index.find(album_path)
        source = (old.source if old is not None else self._source_of(album_path)) or ""
        album = read_album(album_path, source, None, self.thumbs_dir)
        if old is not None and not album.has_cover:
            album.thumb = old.thumb
        if old is not None:
            album.state = old.state
        index.albums = [a for a in index.albums if norm_path(a.path) != key] + [album]
        index.albums.sort(key=lambda a: (a.artist.lower(), a.name.lower()))
        if archive is not None:
            self.match(index, archive)
        self.save(index)
        return index

    # --- Abgleich mit dem Archiv ---

    def match(self, index: LibraryIndex, archive) -> LibraryIndex:
        """Setzt den Zustand jedes Albums gegenueber dem Cover-Archiv und traegt
        verschobene Alben unter ihrem neuen Pfad ein (alte Bibliothekspfade
        ohne Ordner werden entfernt). Archiv-Eintraege, deren Bibliotheks-
        ordner weg ist, landen in index.orphaned."""
        records = {r.id: r for r in archive.all_albums()} if archive is not None else {}
        by_fingerprint = {r.fingerprint: r for r in records.values() if getattr(r, "fingerprint", "")}
        present = {norm_path(a.path) for a in index.albums}
        for album in index.albums:
            if album.offline:
                album.state = STATE_OFFLINE
                continue
            record = records.get(album.album_id) if album.album_id else None
            if record is None and album.fingerprint and not album.album_id and len(album.tracks) >= MIN_FINGERPRINT_TRACKS:
                record = by_fingerprint.get(album.fingerprint)
            if record is None:
                album.state = STATE_NEW
                continue
            known_paths = {norm_path(p) for p in record.paths}
            if norm_path(album.path) in known_paths:
                album.state = STATE_LINKED
            else:
                album.state = STATE_MOVED
                # Nur Pfade entfernen, die es wirklich nicht mehr gibt - nicht solche,
                # die beim Einlesen bloss nicht erreichbar waren
                record.paths = [p for p in record.paths
                                if not (self._in_sources(p) and norm_path(p) not in present
                                        and not os.path.isdir(p))]
                record.paths.append(album.path)
                if not record.title:
                    record.title = album.title
                if not record.artist:
                    record.artist = album.artist
                archive.save_album(record)
            if not album.album_id and record is not None:
                album.album_id = record.id  # ueber den Fingerabdruck erkannt, Tag fehlt noch
        linked_ids = {a.album_id for a in index.albums if a.album_id}
        index.orphaned = []
        for record in records.values():
            library_paths = [p for p in record.paths if self._in_sources(p)]
            if library_paths and record.id not in linked_ids and not any(
                    is_offline(self._source_of(p)) for p in library_paths):
                index.orphaned.append({"id": record.id, "title": record.title, "artist": record.artist,
                                       "paths": library_paths})
        return index

    def _source_of(self, path: str) -> str:
        key = norm_path(path)
        for source in self.sources:
            s = norm_path(source)
            if key == s or key.startswith(s + "/"):
                return source
        return ""

    def _in_sources(self, path: str) -> bool:
        return bool(self._source_of(path))


def sd_album_ids(folders) -> Dict[str, int]:
    """Album-ID -> Ordnernummer fuer die Ordner einer SD-Karte (card.json zuerst,
    sonst der ID-Tag der ersten Tracks)"""
    from core import album_identity, card_store
    result: Dict[str, int] = {}
    for folder in folders:
        if folder.index is None:
            continue
        meta = card_store.load_meta(folder.path)
        album_id = meta.album_id if meta is not None else ""
        if not album_id and folder.tracks:
            album_id = album_identity.read_album_id([t.filepath for t in folder.tracks]) or ""
        if album_id and album_id not in result:
            result[album_id] = folder.index
    return result


def format_time(seconds: float) -> str:
    """Spieldauer eines Stuecks als M:SS bzw. H:MM:SS (z.B. 3:07, 1:02:45)"""
    total = int(round(seconds or 0))
    hours, rest = divmod(total, 3600)
    minutes, secs = divmod(rest, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes}:{secs:02d}"


def format_duration(seconds: float) -> str:
    """Gesamtdauer eines Albums/Ordners als M:SS bzw. H:MM:SS; leer unter einer Sekunde"""
    return format_time(seconds) if seconds and seconds >= 1 else ""
