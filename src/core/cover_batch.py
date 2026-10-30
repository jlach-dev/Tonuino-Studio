"""
Stapelsuche: Album-Cover fuer alle Bibliotheksalben ohne Cover

Stufen je Album:
1. Archiv: ist dort ein Album-Cover gesetzt, wird es genommen (sicher).
2. MusicBrainz-Release-ID in den Tags: Front-Cover aus dem Cover Art Archive
   zu genau diesem Release (sicher).
3. Suche bei Deezer, iTunes, Spotify (mit Zugangsdaten) und MusicBrainz mit
   Album-Tag und Interpret. Jeder Treffer wird bewertet (Titel, Interpret,
   Trackanzahl); nur ein klarer Spitzenreiter gilt als sicher.

Geschrieben wird nie automatisch: die Vorschlaege gehen durch den Pruefdialog
(gui/cover_proposal_dialog.py), erst "Uebernehmen" schreibt die angehakten
Cover in die Dateien, als cover.jpg und ins Archiv.
"""

import logging
import urllib.parse
from dataclasses import dataclass, field
from typing import Callable, Iterable, List, Optional

from core import cover_sources, musicbrainz, tag_lookup
from core.cover_archive import KIND_ONLINE, ROLE_ALBUM_COVER, CoverArchive, sha1
from core.library import LibraryAlbum, LibraryIndex, write_album_cover, write_album_id_to_album
from core.library_import import EmbedResult, archive_album_cover
from core.tag_lookup import title_similarity

log = logging.getLogger(__name__)

SOURCE_ARCHIVE = "Archiv"
SOURCE_RELEASE_ID = "MusicBrainz (Release-ID)"
SEARCH_SOURCES = (cover_sources.SOURCE_DEEZER, cover_sources.SOURCE_ITUNES, cover_sources.SOURCE_SPOTIFY,
                  cover_sources.SOURCE_MUSICBRAINZ)

CERTAIN_MIN = 70.0  # Mindestpunkte fuer "sicher"
CERTAIN_LEAD = 10.0  # Vorsprung vor dem zweitbesten Treffer
MAX_HITS_PER_SOURCE = 5


@dataclass
class CoverHit:
    source: str
    title: str
    artist: str
    image_url: str
    page_url: str = ""
    track_count: int = 0
    score: float = 0.0
    reasons: List[str] = field(default_factory=list)

    @property
    def label(self) -> str:
        text = f"{self.source} · {self.title}" if self.title else self.source
        return f"{text} – {self.artist}" if self.artist else text


@dataclass
class AlbumProposal:
    album: LibraryAlbum
    hit: Optional[CoverHit] = None
    data: Optional[bytes] = None  # Bilddaten des Vorschlags (Original der Quelle)
    certain: bool = False
    note: str = ""
    alternatives: List[CoverHit] = field(default_factory=list)

    @property
    def usable(self) -> bool:
        return self.hit is not None and self.data is not None


# --- Treffer sammeln -------------------------------------------------------------------------

def _deezer_hits(title: str, artist: str) -> List[CoverHit]:
    data = cover_sources._get_json("https://api.deezer.com/search/album?" + urllib.parse.urlencode(
        {"q": f"{artist} {title}".strip(), "limit": MAX_HITS_PER_SOURCE}))
    hits = []
    for album in data.get("data", [])[:MAX_HITS_PER_SOURCE]:
        url = album.get("cover_xl") or album.get("cover_big")
        if url:
            hits.append(CoverHit(cover_sources.SOURCE_DEEZER, album.get("title", ""),
                                 (album.get("artist") or {}).get("name", ""), url, album.get("link", ""),
                                 int(album.get("nb_tracks") or 0)))
    return hits


def _itunes_hits(title: str, artist: str, country: str = "DE") -> List[CoverHit]:
    hits = []
    for entity in ("album", "audiobook"):
        data = cover_sources._get_json("https://itunes.apple.com/search?" + urllib.parse.urlencode(
            {"term": f"{artist} {title}".strip(), "entity": entity, "country": country, "limit": MAX_HITS_PER_SOURCE}))
        for item in data.get("results", []):
            art = item.get("artworkUrl100", "")
            if art:
                hits.append(CoverHit(cover_sources.SOURCE_ITUNES, item.get("collectionName", ""),
                                     item.get("artistName", ""), art.replace("100x100bb", "1200x1200bb"),
                                     item.get("collectionViewUrl", ""), int(item.get("trackCount") or 0)))
        if hits:
            break
    return hits[:MAX_HITS_PER_SOURCE]


def _spotify_hits(title: str, artist: str, market: str = "DE") -> List[CoverHit]:
    data = tag_lookup._spotify_get("https://api.spotify.com/v1/search?" + urllib.parse.urlencode(
        {"q": f"{artist} {title}".strip(), "type": "album", "market": market, "limit": MAX_HITS_PER_SOURCE}))
    hits = []
    for album in (data.get("albums") or {}).get("items", []):
        images = sorted(album.get("images", []), key=lambda i: i.get("width") or 0, reverse=True)
        if not images:
            continue
        artists = ", ".join(a.get("name", "") for a in album.get("artists", []) if a.get("name"))
        hits.append(CoverHit(cover_sources.SOURCE_SPOTIFY, album.get("name", ""), artists, images[0]["url"],
                             (album.get("external_urls") or {}).get("spotify", ""),
                             int(album.get("total_tracks") or 0)))
    return hits


def release_front_url(release_id: str, size: int = 1200) -> str:
    return f"https://coverartarchive.org/release/{release_id}/front-{size}"


def _musicbrainz_hits(title: str, artist: str) -> List[CoverHit]:
    hits = []
    for release in musicbrainz.search_releases(title, artist, limit=MAX_HITS_PER_SOURCE):
        hits.append(CoverHit(cover_sources.SOURCE_MUSICBRAINZ, release.title, release.artist,
                             release_front_url(release.id), f"https://musicbrainz.org/release/{release.id}",
                             release.track_count))
    return hits


SEARCHERS = {
    cover_sources.SOURCE_DEEZER: _deezer_hits,
    cover_sources.SOURCE_ITUNES: _itunes_hits,
    cover_sources.SOURCE_SPOTIFY: _spotify_hits,
    cover_sources.SOURCE_MUSICBRAINZ: _musicbrainz_hits,
}


def collect_hits(title: str, artist: str, sources: Iterable[str]) -> List[CoverHit]:
    hits: List[CoverHit] = []
    for source in sources:
        searcher = SEARCHERS.get(source)
        if searcher is None:
            continue
        if source == cover_sources.SOURCE_SPOTIFY and not tag_lookup.spotify_available():
            continue
        try:
            hits.extend(searcher(title, artist))
        except Exception as e:
            log.warning("Cover-Stapelsuche %s fehlgeschlagen: %s", source, e)
    return hits


# --- Bewertung -------------------------------------------------------------------------------

def score_hit(album: LibraryAlbum, hit: CoverHit) -> float:
    """0..100: Titel (bis 55), Interpret (bis 20), Trackanzahl (+25 / -15)"""
    reasons = []
    title_score = title_similarity(album.title, hit.title) if album.title and hit.title else 0.0
    score = 55.0 * title_score
    reasons.append(f"Titel {int(title_score * 100)} %")
    if album.artist and hit.artist:
        artist_score = title_similarity(album.artist, hit.artist)
        score += 20.0 * artist_score
        reasons.append(f"Interpret {int(artist_score * 100)} %")
    else:
        score += 10.0  # unbekannt: neutral
    if hit.track_count and album.track_count:
        if hit.track_count == album.track_count:
            score += 25.0
            reasons.append(f"{hit.track_count} Tracks ✓")
        else:
            score -= 15.0
            reasons.append(f"{hit.track_count} statt {album.track_count} Tracks")
    hit.score = max(0.0, min(100.0, round(score, 1)))
    hit.reasons = reasons
    return hit.score


def rank_hits(album: LibraryAlbum, hits: List[CoverHit]) -> List[CoverHit]:
    for hit in hits:
        score_hit(album, hit)
    # bei gleicher Punktzahl entscheidet die Reihenfolge der Quellen
    return sorted(hits, key=lambda h: -h.score)


def is_certain(ranked: List[CoverHit]) -> bool:
    if not ranked or ranked[0].score < CERTAIN_MIN:
        return False
    return len(ranked) == 1 or ranked[0].score - ranked[1].score >= CERTAIN_LEAD


# --- Vorschlag je Album ----------------------------------------------------------------------

def propose(album: LibraryAlbum, sources: Iterable[str] = SEARCH_SOURCES,
            download: Callable[[str], Optional[bytes]] = cover_sources.download_image,
            archive: Optional[CoverArchive] = None) -> AlbumProposal:
    proposal = AlbumProposal(album=album)
    # 1. Archiv
    if archive is not None and album.album_id:
        data = archive_album_cover(archive, album.album_id)
        if data:
            proposal.hit = CoverHit(SOURCE_ARCHIVE, album.title, album.artist, "", score=100.0,
                                    reasons=["im Cover-Manager gesetzt"])
            proposal.data, proposal.certain, proposal.note = data, True, "Album-Cover aus dem Archiv"
            return proposal
    # 2. Release-ID aus den Tags
    if album.mb_release_id:
        data = download(release_front_url(album.mb_release_id))
        if data:
            proposal.hit = CoverHit(SOURCE_RELEASE_ID, album.title, album.artist,
                                    release_front_url(album.mb_release_id),
                                    f"https://musicbrainz.org/release/{album.mb_release_id}", album.track_count,
                                    100.0, ["Release-ID in den Tags"])
            proposal.data, proposal.certain, proposal.note = data, True, "Cover Art Archive zur Release-ID"
            return proposal
    # 3. Suche mit Bewertung
    if not album.title:
        proposal.note = "kein Album-Tag – zuerst Tags nachtragen"
        return proposal
    ranked = rank_hits(album, collect_hits(album.title, album.artist, sources))
    if not ranked:
        proposal.note = "nichts gefunden"
        return proposal
    certain = is_certain(ranked)
    for position, hit in enumerate(ranked):
        data = download(hit.image_url)
        if data:
            proposal.hit, proposal.data = hit, data
            proposal.alternatives = [h for h in ranked if h is not hit][:5]
            proposal.certain = certain and position == 0
            proposal.note = ("sicher" if proposal.certain else "bitte prüfen") + " · " + ", ".join(hit.reasons)
            return proposal
    proposal.note = "Bilder nicht ladbar"
    return proposal


def propose_all(index: LibraryIndex, archive: Optional[CoverArchive], sources: Iterable[str] = SEARCH_SOURCES,
                progress: Optional[Callable[[int, int, str], None]] = None,
                cancelled: Optional[Callable[[], bool]] = None,
                download: Callable[[str], Optional[bytes]] = cover_sources.download_image) -> List[AlbumProposal]:
    """Vorschlaege fuer alle Alben ohne Cover (erreichbar, mit Dateien)"""
    todo = albums_without_cover(index)
    sources = list(sources)
    proposals = []
    for i, album in enumerate(todo, start=1):
        if cancelled is not None and cancelled():
            break
        if progress is not None:
            progress(i, len(todo), album.name)
        try:
            proposals.append(propose(album, sources, download, archive))
        except Exception as e:
            log.warning("Cover-Vorschlag fuer %s fehlgeschlagen: %s", album.name, e)
            proposals.append(AlbumProposal(album=album, note=f"Fehler: {e}"))
    return proposals


def albums_without_cover(index: LibraryIndex) -> List[LibraryAlbum]:
    return [a for a in index.albums if not a.offline and not a.has_cover and a.tracks]


# --- Uebernehmen -----------------------------------------------------------------------------

def apply_proposals(proposals: Iterable[AlbumProposal], archive: Optional[CoverArchive],
                    progress: Optional[Callable[[int, int, str], None]] = None,
                    write_album_ids: bool = False) -> EmbedResult:
    """Schreibt die Cover der uebergebenen (angehakten) Vorschlaege: in die
    Dateien, als cover.jpg und ins Archiv (Rolle Album-Cover). Die Album-ID
    kommt nur mit write_album_ids (Erlaubnis des Nutzers) in die Dateien."""
    import io
    from PIL import Image
    from core.card_layout import album_cover_jpeg
    result = EmbedResult()
    chosen = [p for p in proposals if p.usable]
    for i, proposal in enumerate(chosen, start=1):
        album = proposal.album
        if progress is not None:
            progress(i, len(chosen), album.name)
        try:
            with Image.open(io.BytesIO(proposal.data)) as img:
                img.load()
                jpeg = album_cover_jpeg(img)
        except Exception as e:
            log.warning("Cover von %s nicht lesbar: %s", album.name, e)
            result.skipped += 1
            continue
        written = write_album_cover(list(album.tracks), album.path, jpeg)
        if not written:
            result.skipped += 1
            continue
        result.albums += 1
        result.tracks += written
        album.has_cover = True
        if archive is not None and proposal.hit is not None and proposal.hit.source != SOURCE_ARCHIVE:
            try:
                _archive_cover(archive, album, proposal, jpeg, write_album_ids)
            except Exception as e:
                log.warning("Cover von %s konnte nicht archiviert werden: %s", album.name, e)
    return result


def _archive_cover(archive: CoverArchive, album: LibraryAlbum, proposal: AlbumProposal, jpeg: bytes,
                   write_album_ids: bool = False):
    from core import album_identity
    album_id = album.album_id
    if not album_id:
        album_id = album_identity.new_album_id()
        if write_album_ids:
            write_album_id_to_album(album, album_id)
        # ohne Tag haelt der Bibliothekspfad im Archiv-Eintrag die Verknuepfung
    archive.ensure_album(album_id, album.title, album.artist, path=album.path)
    hit = proposal.hit
    entry = archive.add_cover(album_id, proposal.data, KIND_ONLINE, source=hit.image_url or hit.page_url,
                              provider=hit.source)
    archive.set_role(album_id, entry.id, ROLE_ALBUM_COVER)
    record = archive.load_album(album_id)
    if record is not None:
        record.album_cover_sha1 = sha1(jpeg)
        archive.save_album(record)
