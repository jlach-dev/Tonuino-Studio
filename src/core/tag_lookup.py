"""
Tags von MusicBrainz (bzw. Deezer/iTunes als Ersatz) fuer einen SD-Ordner

Typischer Fall: Podcast-/Radio-Downloads, bei denen TALB nur den Feed nennt
("MausHörspiel lang"), TPE1 den Autor und die Titel das Muster
"Die kleine Hexe (1/2): Heia Walpurgisnacht | MausHörspiel lang (07.12.2024)"
haben. Ablauf:

1. search_candidates: Releases mit Trackliste in den gewaehlten Quellen suchen
   (Suchbegriff wie bei der Cover-Suche: erkannte Reihe, sonst Album-Tag ...)
2. load_tracks: Trackliste eines Kandidaten nachladen
3. match_tracks: lokale Tracks den Release-Tracks zuordnen - zuerst ueber
   Position und Laenge (±3 s), dann nur Laenge, dann Titelaehnlichkeit
4. build_diff / apply_diff: alt -> neu je Feld, Schreiben nach Bestaetigung
   (ID3 ueber MetadataManager, MusicBrainz-IDs als TXXX wie bei Picard)
"""

import difflib
import logging
import re
import urllib.parse
from dataclasses import dataclass, field
from typing import Callable, Dict, Iterable, List, Optional

from core import album_identity, cover_sources, musicbrainz
from core.cover_research import FolderInfo, clean_track_title, suggest_search_title
from core.metadata import TrackMetadata
from core.musicbrainz import ReleaseHit, ReleaseTrack

log = logging.getLogger(__name__)

SOURCE_MUSICBRAINZ = "MusicBrainz"
SOURCE_DEEZER = "Deezer"
SOURCE_ITUNES = "iTunes"
ALL_SOURCES = (SOURCE_MUSICBRAINZ, SOURCE_DEEZER, SOURCE_ITUNES)

DURATION_TOLERANCE = 3.0  # Sekunden
MIN_TITLE_SIMILARITY = 0.6

# Zuordnungsarten (fuer die Anzeige)
MATCH_POSITION_DURATION = "Position + Länge"
MATCH_DURATION = "Länge"
MATCH_TITLE = "Titel"
MATCH_POSITION = "nur Position"
MATCH_NONE = ""

# Felder der Diff-Tabelle: (Schluessel, Spaltentitel)
FIELD_ALBUM = "album"
FIELD_ARTIST = "artist"
FIELD_ALBUM_ARTIST = "album_artist"
FIELD_TITLE = "title"
FIELD_TRACK = "track_number"
FIELD_YEAR = "year"
FIELD_MB_IDS = "mb_ids"
FIELDS = [
    (FIELD_ALBUM, "Album"),
    (FIELD_ARTIST, "Interpret"),
    (FIELD_ALBUM_ARTIST, "Albuminterpret"),
    (FIELD_TITLE, "Titel"),
    (FIELD_TRACK, "Tracknummer"),
    (FIELD_YEAR, "Jahr"),
    (FIELD_MB_IDS, "MusicBrainz-IDs"),
]

# TXXX-Beschreibungen wie bei MusicBrainz Picard
MB_RELEASE_TRACK_ID_DESC = "MusicBrainz Release Track Id"
MB_ALBUM_ARTIST_ID_DESC = "MusicBrainz Album Artist Id"
MB_ARTIST_ID_DESC = "MusicBrainz Artist Id"


@dataclass
class LocalTrack:
    """Ein Track des SD-Ordners mit seinen aktuellen Tags"""
    filepath: str
    filename: str
    index: int  # Dateinummer (001.mp3 -> 1)
    meta: TrackMetadata


@dataclass
class ReleaseCandidate:
    """Ein Album einer Quelle; die Trackliste wird bei Bedarf nachgeladen"""
    source: str
    id: str
    title: str
    artist: str = ""
    date: str = ""
    track_count: int = 0
    url: str = ""
    score: int = 0
    release_group_id: str = ""
    artist_ids: List[str] = field(default_factory=list)
    tracks: List[ReleaseTrack] = field(default_factory=list)
    tracks_loaded: bool = False

    @property
    def year(self) -> str:
        return (self.date or "")[:4]

    @property
    def label(self) -> str:
        parts = [self.title]
        if self.artist:
            parts.append(self.artist)
        if self.year:
            parts.append(self.year)
        if self.track_count:
            parts.append(f"{self.track_count} Tracks")
        return f"{self.source}: " + " · ".join(parts)


@dataclass
class TrackDiff:
    """Alt -> neu fuer einen lokalen Track; new ist leer ohne Zuordnung"""
    local: LocalTrack
    release_track: Optional[ReleaseTrack]
    method: str
    old: Dict[str, str] = field(default_factory=dict)
    new: Dict[str, str] = field(default_factory=dict)
    # Werte, die geschrieben werden (nicht nur angezeigt)
    track_number: int = 0
    total_tracks: int = 0
    mb_ids: Dict[str, str] = field(default_factory=dict)  # TXXX-Beschreibung -> Wert

    @property
    def matched(self) -> bool:
        return self.release_track is not None

    def changed(self, key: str) -> bool:
        return self.matched and self.new.get(key, "") != self.old.get(key, "") and bool(self.new.get(key, ""))


# --- Lokale Tracks ---------------------------------------------------------------------------

def read_local_tracks(folder, metadata_manager, cancelled: Callable[[], bool] = lambda: False) -> List[LocalTrack]:
    result = []
    for track in folder.tracks:
        if cancelled():
            break
        result.append(LocalTrack(track.filepath, track.filename, track.index, metadata_manager.read_metadata(track.filepath)))
    return result


def folder_info(local: Iterable[LocalTrack]) -> FolderInfo:
    info = FolderInfo()
    for track in local:
        meta = track.meta
        info.album = info.album or meta.album
        info.artist = info.artist or meta.album_artist or meta.artist
        info.album_artist = info.album_artist or meta.album_artist
        if meta.album and meta.album not in info.albums:
            info.albums.append(meta.album)
        if meta.title:
            info.titles.append(meta.title)
    return info


def suggest_search(local: Iterable[LocalTrack]) -> tuple:
    """(Titel, Interpret) fuer die Suche aus den vorhandenen Tags"""
    info = folder_info(local)
    return suggest_search_title(info), info.artist


# --- Suche -----------------------------------------------------------------------------------

def _from_release_hit(hit: ReleaseHit) -> ReleaseCandidate:
    return ReleaseCandidate(SOURCE_MUSICBRAINZ, hit.id, hit.title, hit.artist, hit.date, hit.track_count,
                            f"https://musicbrainz.org/release/{hit.id}", hit.score, hit.release_group_id,
                            list(hit.artist_ids), list(hit.tracks), bool(hit.tracks))


def search_musicbrainz(title: str, artist: str = "", limit: int = 10) -> List[ReleaseCandidate]:
    return [_from_release_hit(hit) for hit in musicbrainz.search_releases(title, artist, limit)]


def search_deezer(title: str, artist: str = "", limit: int = 6) -> List[ReleaseCandidate]:
    query = f"{artist} {title}".strip()
    data = cover_sources._get_json("https://api.deezer.com/search/album?"
                                   + urllib.parse.urlencode({"q": query, "limit": limit}))
    result = []
    for album in data.get("data", [])[:limit]:
        if not album.get("id"):
            continue
        result.append(ReleaseCandidate(SOURCE_DEEZER, str(album["id"]), album.get("title", ""),
                                       (album.get("artist") or {}).get("name", ""), album.get("release_date", ""),
                                       int(album.get("nb_tracks") or 0), album.get("link", "")))
    return result


def search_itunes(title: str, artist: str = "", limit: int = 6, country: str = "DE") -> List[ReleaseCandidate]:
    query = f"{artist} {title}".strip()
    data = cover_sources._get_json("https://itunes.apple.com/search?" + urllib.parse.urlencode(
        {"term": query, "entity": "album", "country": country, "limit": limit}))
    result = []
    for item in data.get("results", [])[:limit]:
        if not item.get("collectionId"):
            continue
        result.append(ReleaseCandidate(SOURCE_ITUNES, str(item["collectionId"]), item.get("collectionName", ""),
                                       item.get("artistName", ""), item.get("releaseDate", ""),
                                       int(item.get("trackCount") or 0), item.get("collectionViewUrl", "")))
    return result


SEARCHERS = {
    SOURCE_MUSICBRAINZ: search_musicbrainz,
    SOURCE_DEEZER: search_deezer,
    SOURCE_ITUNES: search_itunes,
}


def search_candidates(title: str, artist: str = "", sources: Iterable[str] = ALL_SOURCES, track_count: int = 0,
                      on_status: Optional[Callable[[str], None]] = None,
                      cancelled: Callable[[], bool] = lambda: False) -> List[ReleaseCandidate]:
    """Kandidaten aller gewaehlten Quellen: passende Trackanzahl zuerst, dann
    in der Reihenfolge der Quellen (MusicBrainz vor Deezer vor iTunes) und
    nach Suchtreffer-Score."""
    if not title.strip():
        return []
    sources = [s for s in ALL_SOURCES if s in set(sources)]
    found: List[ReleaseCandidate] = []
    for rank, source in enumerate(sources):
        if cancelled():
            break
        if on_status:
            on_status(f"Suche bei {source} …")
        try:
            candidates = SEARCHERS[source](title, artist)
        except Exception as e:
            log.warning("Tag-Suche %s fehlgeschlagen: %s", source, e)
            continue
        for order, candidate in enumerate(candidates):
            found.append((rank, order, candidate))
    found.sort(key=lambda item: (bool(track_count) and item[2].track_count != track_count,
                                 item[0], -item[2].score, item[1]))
    return [item[2] for item in found]


# --- Tracklisten -----------------------------------------------------------------------------

def _load_deezer_tracks(candidate: ReleaseCandidate):
    if not candidate.date:
        try:
            album = cover_sources._get_json(f"https://api.deezer.com/album/{candidate.id}")
            candidate.date = album.get("release_date", "") or candidate.date
        except Exception as e:
            log.info("Deezer-Album %s nicht ladbar: %s", candidate.id, e)
    data = cover_sources._get_json(f"https://api.deezer.com/album/{candidate.id}/tracks")
    tracks = []
    for position, item in enumerate(data.get("data", []), start=1):
        tracks.append(ReleaseTrack(position, item.get("title", ""), (item.get("artist") or {}).get("name", "")
                                   or candidate.artist, float(item.get("duration") or 0)))
    return tracks


def _load_itunes_tracks(candidate: ReleaseCandidate, country: str = "DE"):
    data = cover_sources._get_json("https://itunes.apple.com/lookup?" + urllib.parse.urlencode(
        {"id": candidate.id, "entity": "song", "country": country}))
    songs = [item for item in data.get("results", []) if item.get("wrapperType") == "track"]
    songs.sort(key=lambda s: (int(s.get("discNumber") or 1), int(s.get("trackNumber") or 0)))
    tracks = []
    for position, item in enumerate(songs, start=1):
        tracks.append(ReleaseTrack(position, item.get("trackName", ""), item.get("artistName", "") or candidate.artist,
                                   float(item.get("trackTimeMillis") or 0) / 1000.0))
    return tracks


def load_tracks(candidate: ReleaseCandidate) -> ReleaseCandidate:
    """Laedt die Trackliste nach (einmalig); MusicBrainz liefert dabei auch
    Datum, Release-Group und Interpreten-IDs."""
    if candidate.tracks_loaded:
        return candidate
    if candidate.source == SOURCE_MUSICBRAINZ:
        hit = musicbrainz.lookup_release(candidate.id)
        candidate.tracks = hit.tracks
        candidate.date = hit.date or candidate.date
        candidate.title = hit.title or candidate.title
        candidate.artist = hit.artist or candidate.artist
        candidate.release_group_id = hit.release_group_id or candidate.release_group_id
        candidate.artist_ids = hit.artist_ids or candidate.artist_ids
    elif candidate.source == SOURCE_DEEZER:
        candidate.tracks = _load_deezer_tracks(candidate)
    elif candidate.source == SOURCE_ITUNES:
        candidate.tracks = _load_itunes_tracks(candidate)
    candidate.track_count = len(candidate.tracks) or candidate.track_count
    candidate.tracks_loaded = True
    return candidate


# --- Zuordnung -------------------------------------------------------------------------------

_EPISODE_SPLIT = re.compile(r"\(\d+\s*/\s*\d+\)\s*[:\-–]\s*|\b(?:folge|teil|episode|kapitel)\s*\d+\s*[:\-–.]\s*",
                            re.IGNORECASE)


def _norm(text: str) -> str:
    return re.sub(r"[^\wäöüß]+", " ", (text or "").lower()).strip()


def _title_variants(text: str) -> List[str]:
    """Lesarten eines Tracktitels: ganz (ohne Feed-Anhang), nur der Folgentitel
    nach "(1/2):" bzw. "Folge 3:", und die bereinigte Reihe"""
    base = (text or "").split(" | ")[0]
    variants = [_norm(base)]
    parts = _EPISODE_SPLIT.split(base, maxsplit=1)
    if len(parts) == 2:
        variants.append(_norm(parts[1]))
    variants.append(_norm(clean_track_title(base)))
    return [v for v in dict.fromkeys(variants) if v]


def title_similarity(a: str, b: str) -> float:
    """0..1; vergleicht alle Lesarten beider Titel, die beste zaehlt"""
    best = 0.0
    for x in _title_variants(a):
        for y in _title_variants(b):
            if x == y or (len(x) >= 4 and len(y) >= 4 and (x in y or y in x)):
                return 1.0
            best = max(best, difflib.SequenceMatcher(None, x, y).ratio())
    return best


def _durations_match(local: float, remote: float) -> bool:
    return local > 0 and remote > 0 and abs(local - remote) <= DURATION_TOLERANCE


def match_tracks(local: List[LocalTrack], release_tracks: List[ReleaseTrack]) -> List[tuple]:
    """[(ReleaseTrack|None, Zuordnungsart)] in der Reihenfolge der lokalen
    Tracks. Jeder Release-Track wird hoechstens einmal vergeben."""
    by_position = {t.position: t for t in release_tracks}
    used = set()
    result: List[Optional[tuple]] = [None] * len(local)

    def take(i, track, method):
        used.add(id(track))
        result[i] = (track, method)

    # 1. Position (Tracknummer-Tag, sonst Dateinummer bzw. Reihenfolge) und Laenge
    for i, item in enumerate(local):
        position = item.meta.track_number or item.index or (i + 1)
        candidate = by_position.get(position)
        if candidate is not None and id(candidate) not in used \
                and _durations_match(item.meta.duration, candidate.length):
            take(i, candidate, MATCH_POSITION_DURATION)

    # 2. nur Laenge, wenn eindeutig
    for i, item in enumerate(local):
        if result[i] is not None:
            continue
        same_length = [t for t in release_tracks if id(t) not in used and _durations_match(item.meta.duration, t.length)]
        if len(same_length) == 1:
            take(i, same_length[0], MATCH_DURATION)

    # 3. Titelaehnlichkeit
    for i, item in enumerate(local):
        if result[i] is not None or not item.meta.title:
            continue
        best, best_score = None, 0.0
        for track in release_tracks:
            if id(track) in used:
                continue
            score = title_similarity(item.meta.title, track.title)
            if score > best_score:
                best, best_score = track, score
        if best is not None and best_score >= MIN_TITLE_SIMILARITY:
            take(i, best, MATCH_TITLE)

    # 4. gleiche Anzahl: Rest nach Position (zur Pruefung durch den Nutzer)
    if len(local) == len(release_tracks):
        for i, item in enumerate(local):
            if result[i] is not None:
                continue
            candidate = by_position.get(i + 1)
            if candidate is not None and id(candidate) not in used:
                take(i, candidate, MATCH_POSITION)

    return [entry if entry is not None else (None, MATCH_NONE) for entry in result]


# --- Diff ------------------------------------------------------------------------------------

def _old_values(meta: TrackMetadata) -> Dict[str, str]:
    track = ""
    if meta.track_number:
        track = f"{meta.track_number}/{meta.total_tracks}" if meta.total_tracks else str(meta.track_number)
    return {
        FIELD_ALBUM: meta.album, FIELD_ARTIST: meta.artist, FIELD_ALBUM_ARTIST: meta.album_artist,
        FIELD_TITLE: meta.title, FIELD_TRACK: track, FIELD_YEAR: meta.year, FIELD_MB_IDS: "",
    }


def build_diff(local: List[LocalTrack], candidate: ReleaseCandidate) -> List[TrackDiff]:
    """Alt -> neu je Track. Ohne Zuordnung bleibt new leer. Fuer MusicBrainz
    werden die IDs (Release, Release-Group, Track, Interpreten) vorgesehen."""
    matches = match_tracks(local, candidate.tracks)
    total = candidate.track_count or len(candidate.tracks)
    diffs = []
    for item, (release_track, method) in zip(local, matches):
        diff = TrackDiff(item, release_track, method, _old_values(item.meta))
        if release_track is not None:
            diff.track_number = release_track.position
            diff.total_tracks = total
            diff.new = {
                FIELD_ALBUM: candidate.title,
                FIELD_ARTIST: release_track.artist or candidate.artist,
                FIELD_ALBUM_ARTIST: candidate.artist,
                FIELD_TITLE: release_track.title,
                FIELD_TRACK: f"{release_track.position}/{total}" if total else str(release_track.position),
                FIELD_YEAR: candidate.year,
                FIELD_MB_IDS: "",
            }
            if candidate.source == SOURCE_MUSICBRAINZ:
                ids = {album_identity.MB_ALBUM_ID_DESC: candidate.id}
                if candidate.release_group_id:
                    ids[album_identity.MB_RELEASE_GROUP_ID_DESC] = candidate.release_group_id
                if release_track.track_id:
                    ids[MB_RELEASE_TRACK_ID_DESC] = release_track.track_id
                if candidate.artist_ids:
                    ids[MB_ALBUM_ARTIST_ID_DESC] = "; ".join(candidate.artist_ids)
                if release_track.artist_ids:
                    ids[MB_ARTIST_ID_DESC] = "; ".join(release_track.artist_ids)
                diff.mb_ids = ids
                diff.new[FIELD_MB_IDS] = f"Release {candidate.id[:8]}…"
                existing = album_identity.read_mb_ids(item.filepath)
                if existing["release_id"]:
                    diff.old[FIELD_MB_IDS] = f"Release {existing['release_id'][:8]}…"
        diffs.append(diff)
    return diffs


def apply_diff(diffs: List[TrackDiff], selection: Dict[int, Iterable[str]], metadata_manager) -> int:
    """Schreibt die gewaehlten Felder: selection = {Zeilenindex: Felder}.
    Liefert die Anzahl der geaenderten Dateien."""
    written = 0
    for row, fields in selection.items():
        if row >= len(diffs):
            continue
        diff = diffs[row]
        fields = {f for f in fields if diff.matched}
        if not fields:
            continue
        kwargs = {}
        if FIELD_ALBUM in fields:
            kwargs["album"] = diff.new[FIELD_ALBUM]
        if FIELD_ARTIST in fields:
            kwargs["artist"] = diff.new[FIELD_ARTIST]
        if FIELD_ALBUM_ARTIST in fields:
            kwargs["album_artist"] = diff.new[FIELD_ALBUM_ARTIST]
        if FIELD_TITLE in fields:
            kwargs["title"] = diff.new[FIELD_TITLE]
        if FIELD_TRACK in fields:
            kwargs["track_number"] = diff.track_number
            kwargs["total_tracks"] = diff.total_tracks
        if FIELD_YEAR in fields and diff.new[FIELD_YEAR]:
            kwargs["year"] = diff.new[FIELD_YEAR]
        ok = True
        if kwargs:
            ok = metadata_manager.write_metadata(diff.local.filepath, **kwargs)
        if FIELD_MB_IDS in fields and diff.mb_ids:
            for desc, value in diff.mb_ids.items():
                ok = album_identity.write_txxx(diff.local.filepath, desc, value) and ok
        if ok and (kwargs or (FIELD_MB_IDS in fields and diff.mb_ids)):
            written += 1
    return written
