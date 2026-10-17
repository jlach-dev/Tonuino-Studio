"""
MusicBrainz und Cover Art Archive (frei fuer nicht-kommerzielle Nutzung, ohne Key)

Regeln laut https://musicbrainz.org/doc/MusicBrainz_API/Rate_Limiting: im
Mittel max. 1 Anfrage/s je IP, aussagekraeftiger User-Agent
"Name/Version ( Kontakt )", bei Ueberschreitung antwortet der Server mit 503.
Alle Anfragen laufen deshalb ueber _http_get (prozessweite Drosselung).
"""

import json
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import List, Optional

from core import __version__

USER_AGENT = f"Tonuino-Manager/{__version__} ( https://github.com/jlach-dev/Tonuino-manager )"
HTTP_TIMEOUT = 20

_last_request = 0.0
# Mehrere Worker (Dialog, Batch) koennen gleichzeitig anfragen - die
# Drosselung muss deshalb prozessweit gelten
_lock = threading.Lock()


def _http_get(url: str, accept: str = "application/json", attempts: int = 3) -> bytes:
    """GET mit Drosselung (>= 1 s Abstand); 503 heisst bei MusicBrainz
    "zu schnell" und wird mit wachsender Pause wiederholt."""
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": accept})
    with _lock:
        return _throttled_open(request, attempts)


def _throttled_open(request, attempts: int) -> bytes:
    global _last_request
    for attempt in range(attempts):
        wait = 1.1 * (attempt + 1) - (time.monotonic() - _last_request)
        if wait > 0:
            time.sleep(wait)
        try:
            with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT) as response:
                return response.read()
        except urllib.error.HTTPError as e:
            if e.code != 503 or attempt == attempts - 1:
                raise
        finally:
            _last_request = time.monotonic()
    raise RuntimeError("unreachable")


def _lucene_escape(text: str) -> str:
    return re.sub(r'([+\-!(){}\[\]^"~*?:\\/]|&&|\|\|)', r"\\\1", text)


@dataclass
class ReleaseGroupHit:
    id: str
    title: str
    artist: str = ""
    date: str = ""
    score: int = 0

    @property
    def label(self) -> str:
        parts = [self.title]
        if self.artist:
            parts.append(self.artist)
        if self.date:
            parts.append(self.date[:4])
        return " · ".join(parts)


@dataclass
class ReleaseTrack:
    """Ein Track eines Releases (Position fortlaufend ueber alle Medien)"""
    position: int
    title: str
    artist: str = ""
    length: float = 0.0  # Sekunden, 0 = unbekannt
    recording_id: str = ""
    track_id: str = ""
    artist_ids: List[str] = field(default_factory=list)


@dataclass
class ReleaseHit:
    """Ein konkretes Release (Ausgabe) mit Trackliste - Grundlage fuer das Tagging"""
    id: str
    title: str
    artist: str = ""
    date: str = ""
    track_count: int = 0
    release_group_id: str = ""
    artist_ids: List[str] = field(default_factory=list)
    score: int = 0
    tracks: List[ReleaseTrack] = field(default_factory=list)

    @property
    def label(self) -> str:
        parts = [self.title]
        if self.artist:
            parts.append(self.artist)
        if self.date:
            parts.append(self.date[:4])
        if self.track_count:
            parts.append(f"{self.track_count} Tracks")
        return " · ".join(parts)


@dataclass
class CoverArtImage:
    """Ein Bild aus dem Cover Art Archive"""
    release_group: ReleaseGroupHit
    image_id: str
    url_500: str
    url_1200: str
    front: bool = False
    types: List[str] = field(default_factory=list)


def search_release_groups(album: str, artist: str = "", limit: int = 8, min_score: int = 70) -> List[ReleaseGroupHit]:
    """Passende Release-Groups, beste zuerst. Mit Interpret ohne Treffer wird
    noch einmal nur nach dem Albumtitel gesucht."""
    if not album.strip():
        return []
    hits = _search(album, artist, limit, min_score)
    if not hits and artist.strip():
        hits = _search(album, "", limit, min_score)
    return hits


def _search(album: str, artist: str, limit: int, min_score: int) -> List[ReleaseGroupHit]:
    query = f'releasegroup:"{_lucene_escape(album)}"'
    if artist.strip():
        query += f' AND artist:"{_lucene_escape(artist)}"'
    url = "https://musicbrainz.org/ws/2/release-group/?" + urllib.parse.urlencode(
        {"query": query, "fmt": "json", "limit": limit})
    data = json.loads(_http_get(url))
    hits = []
    for group in data.get("release-groups", []):
        score = int(group.get("score", 0))
        if score < min_score:
            continue
        credits = group.get("artist-credit") or []
        artist_name = "".join(c.get("name", "") + c.get("joinphrase", "") for c in credits)
        hits.append(ReleaseGroupHit(group["id"], group.get("title", ""), artist_name,
                                    group.get("first-release-date", ""), score))
    return hits


def _credit(credits) -> str:
    return "".join(c.get("name", "") + c.get("joinphrase", "") for c in (credits or []))


def _credit_ids(credits) -> List[str]:
    return [c["artist"]["id"] for c in (credits or []) if (c.get("artist") or {}).get("id")]


def search_releases(album: str, artist: str = "", limit: int = 10, min_score: int = 60) -> List[ReleaseHit]:
    """Passende Releases (ohne Trackliste), beste zuerst. Mit Interpret ohne
    Treffer wird noch einmal nur nach dem Titel gesucht."""
    if not album.strip():
        return []
    hits = _search_releases(album, artist, limit, min_score)
    if not hits and artist.strip():
        hits = _search_releases(album, "", limit, min_score)
    return hits


def _search_releases(album: str, artist: str, limit: int, min_score: int) -> List[ReleaseHit]:
    query = f'release:"{_lucene_escape(album)}"'
    if artist.strip():
        query += f' AND artist:"{_lucene_escape(artist)}"'
    url = "https://musicbrainz.org/ws/2/release/?" + urllib.parse.urlencode(
        {"query": query, "fmt": "json", "limit": limit})
    data = json.loads(_http_get(url))
    hits = []
    for release in data.get("releases", []):
        score = int(release.get("score", 0))
        if score < min_score:
            continue
        hits.append(ReleaseHit(
            release["id"], release.get("title", ""), _credit(release.get("artist-credit")),
            release.get("date", ""), int(release.get("track-count") or 0),
            (release.get("release-group") or {}).get("id", ""), _credit_ids(release.get("artist-credit")), score))
    return hits


def lookup_release(release_id: str) -> ReleaseHit:
    """Release mit vollstaendiger Trackliste (Aufnahmen und Interpreten).
    Bei mehreren Medien (CD 1/2) werden die Positionen fortlaufend gezaehlt."""
    url = (f"https://musicbrainz.org/ws/2/release/{release_id}?"
           + urllib.parse.urlencode({"inc": "recordings+artist-credits+release-groups", "fmt": "json"}))
    data = json.loads(_http_get(url))
    hit = ReleaseHit(data.get("id", release_id), data.get("title", ""), _credit(data.get("artist-credit")),
                     data.get("date", ""), 0, (data.get("release-group") or {}).get("id", ""),
                     _credit_ids(data.get("artist-credit")))
    offset = 0
    for medium in data.get("media", []):
        tracks = medium.get("tracks", [])
        for track in tracks:
            recording = track.get("recording") or {}
            length_ms = track.get("length") or recording.get("length") or 0
            credits = track.get("artist-credit") or recording.get("artist-credit")
            hit.tracks.append(ReleaseTrack(
                offset + int(track.get("position") or len(hit.tracks) - offset + 1),
                track.get("title") or recording.get("title", ""), _credit(credits) or hit.artist,
                float(length_ms) / 1000.0, recording.get("id", ""), track.get("id", ""), _credit_ids(credits)))
        offset += int(medium.get("track-count") or len(tracks))
    hit.tracks.sort(key=lambda t: t.position)
    hit.track_count = len(hit.tracks)
    return hit


def list_cover_art(group: ReleaseGroupHit) -> List[CoverArtImage]:
    """Bilder einer Release-Group (Front zuerst); leer, wenn keine vorhanden"""
    try:
        data = json.loads(_http_get(f"https://coverartarchive.org/release-group/{group.id}"))
    except urllib.error.HTTPError as e:
        # 404 = keine Bilder; 5xx kommt vom Speicher (archive.org) gelegentlich
        # und betrifft nur diese eine Gruppe
        if e.code == 404 or e.code >= 500:
            return []
        raise
    images = []
    for image in data.get("images", []):
        thumbs = image.get("thumbnails", {})
        url_500 = thumbs.get("500") or thumbs.get("large") or image.get("image", "")
        url_1200 = thumbs.get("1200") or image.get("image", "") or url_500
        if not url_500:
            continue
        images.append(CoverArtImage(group, str(image.get("id", "")), url_500.replace("http://", "https://"),
                                    url_1200.replace("http://", "https://"), bool(image.get("front")),
                                    list(image.get("types", []))))
    images.sort(key=lambda i: not i.front)
    return images


def fetch_image(url: str) -> bytes:
    return _http_get(url, accept="image/*")


def fetch_front_cover(release_group_id: str) -> Optional[bytes]:
    """Front-Cover (500 px) einer Release-Group, None wenn keins existiert"""
    try:
        return fetch_image(f"https://coverartarchive.org/release-group/{release_group_id}/front-500")
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise


def release_group_series(release_group_id: str) -> List[dict]:
    """Serien, zu denen eine Release-Group gehoert: [{"id", "name", "number"}]"""
    url = (f"https://musicbrainz.org/ws/2/release-group/{release_group_id}?"
           + urllib.parse.urlencode({"inc": "series-rels", "fmt": "json"}))
    data = json.loads(_http_get(url))
    result = []
    for relation in data.get("relations", []):
        series = relation.get("series")
        if relation.get("target-type") == "series" and series:
            number = (relation.get("attribute-values") or {}).get("number", "")
            result.append({"id": series.get("id", ""), "name": series.get("name", ""), "number": number})
    return result
