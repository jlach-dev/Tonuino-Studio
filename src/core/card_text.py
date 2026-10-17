"""
Kartentext aus den Tags ableiten: Hoerspielreihe (klein) und Folgentitel

Beispiele:
  Tracktitel "Die kleine Hexe (1/2): Heia Walpurgisnacht | MausHörspiel lang (07.12.2024)",
             "Die kleine Hexe (2/2): Der größte Streich | …"  (TALB nur der Feed)
      -> Reihe "Die kleine Hexe", Titel "1. Heia Walpurgisnacht" / "2. Der größte Streich"
  TALB "Folge 39: Fall 77: Die verschleppte Schleppe/Fall 78: Der gefiederte Vergessling",
  TPE1 "Die Fuchsbande"
      -> Reihe "Die Fuchsbande · Folge 39"
         Titel "Fall 77: Die verschleppte Schleppe" / "Fall 78: Der gefiederte Vergessling"
  zwei Folgen auf einer Karte (verschiedene TALB je Track):
      "Bibi Blocksberg - Folge 1: Hexen gibt es doch", "Bibi Blocksberg - Folge 2: ..."
      -> Reihe "Bibi Blocksberg", Titel "Folge 1: Hexen gibt es doch" / "Folge 2: ..."
"""

import re
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

from core.cover_research import CoverBrief, FolderInfo, clean_track_title, parse_episode_titles

MAX_EPISODES = 4

_EPISODE_NO = re.compile(r"^\s*((?:folge|episode|teil|band|vol\.?|nr\.?)\s*\d+)\s*[:.\-–]\s*", re.IGNORECASE)
_SPLIT = re.compile(r"\s*/\s*")


_SEPARATOR = re.compile(r"^\s*[-–:·|]\s*")


def _strip_series(text: str, series: str) -> str:
    """'Die Fuchsbande - Folge 39' -> 'Folge 39' (Reihe steht separat). Nur
    wenn nach der Reihe ein Trennzeichen folgt - 'Eule findet den Beat' bei
    Reihe 'Eule' bleibt unveraendert."""
    if series and text.lower().startswith(series.lower()):
        rest = text[len(series):]
        if _SEPARATOR.match(rest):
            return _SEPARATOR.sub("", rest, count=1) or text
    return text


def main_albums(info: FolderInfo, max_albums: int = MAX_EPISODES) -> List[str]:
    """Die Album-Tags, die den Ordner wirklich ausmachen. Einzelne Tracks aus
    anderen Alben (Bonus-Songs, Singles) duerfen keine eigene Titelzeile
    bekommen: ein Tag zaehlt nur, wenn er mindestens 20 % der Tracks (mind. 2)
    traegt - oder wenn es ohnehin nur Einzeltracks gibt (z.B. ein Track je
    Folge). Ohne Zaehlung (Tests) gelten alle Tags."""
    albums = [a.strip() for a in info.albums if a.strip()]
    if not albums:
        return [info.album.strip()] if info.album.strip() else []
    counts = info.album_counts
    if not counts:
        return albums[:max_albums]
    total = sum(counts.get(a, 0) for a in albums)
    threshold = max(2, round(total * 0.2))
    main = [a for a in albums if counts.get(a, 0) >= threshold]
    if not main:
        # nur Einzeltracks: die ersten (in Trackreihenfolge) nehmen
        main = albums
    return main[:max_albums]


def _split_episodes(album: str) -> List[str]:
    """Doppelfolgen in einem Album-Tag ('Fall 77: …/Fall 78: …') trennen -
    nur wenn alle Teile wie eigene Titel aussehen (z.B. nicht 'AC/DC')"""
    parts = [p.strip() for p in _SPLIT.split(album) if p.strip()]
    if len(parts) > 1 and all(len(p) >= 8 for p in parts):
        return parts
    return [album.strip()]


@dataclass
class WorkDescription:
    """Das Beste, was lokal ueber das Werk bekannt ist - Grundlage fuer
    Recherche- und Bild-Prompt sowie die Anzeige im Cover-Manager"""
    series: str = ""
    episodes: List[str] = field(default_factory=list)
    artist: str = ""
    album_tag: str = ""
    from_track_titles: bool = False  # Reihe/Folgen aus den Tracktiteln (Album-Tag = Feed)
    tags_verified: bool = False  # Tags tragen MusicBrainz-IDs (per Tag-Abgleich bestaetigt)

    @property
    def title(self) -> str:
        """Eine Zeile: Reihe · Folge 1, Folge 2"""
        parts = [p for p in (self.series, ", ".join(self.episodes)) if p]
        return " · ".join(parts)

    @property
    def basis(self) -> str:
        if self.from_track_titles:
            return "aus den Tracktiteln (Album-Tag ist nur der Feed)"
        if self.tags_verified:
            return "aus den Tags, per MusicBrainz bestätigt"
        return "aus den Tags (Album, Interpret)"


def describe_work(info: FolderInfo, brief: Optional[CoverBrief] = None) -> WorkDescription:
    series, title = derive_card_text(info, brief)
    return WorkDescription(
        series=series,
        episodes=[line for line in title.splitlines() if line.strip()],
        artist=(info.album_artist or info.artist).strip(),
        album_tag=info.album.strip(),
        from_track_titles=bool(parse_episode_titles(info.titles)),
        tags_verified=info.tags_verified,
    )


def derive_card_text(info: FolderInfo, brief: Optional[CoverBrief] = None) -> Tuple[str, str]:
    """(Reihe, Titel mit einer Zeile je Folge)"""
    episodes = parse_episode_titles(info.titles)
    if episodes:
        # Der Album-Tag ist bei solchen Downloads meist nur der Feed-Name -
        # Reihe und Folgen stehen in den Tracktiteln
        titles = [f"{e.number}. {e.title}" if len(episodes) > 1 else e.title for e in episodes]
        return episodes[0].series, "\n".join(titles[:MAX_EPISODES])

    series = (info.album_artist or info.artist or (brief.series if brief else "")).strip()
    albums = main_albums(info)

    if len(albums) > 1:
        episodes = [_strip_series(a, series) for a in albums]
    elif albums:
        album = _strip_series(albums[0], series)
        match = _EPISODE_NO.match(album)
        if match:
            # "Folge 39: Fall 77 …" -> Folgennummer wandert zur Reihe
            number = match.group(1).strip()
            series = f"{series} · {number}" if series else number
            album = album[match.end():]
        episodes = _split_episodes(album)
    else:
        if brief is not None and brief.album_title.strip():
            episodes = _split_episodes(_strip_series(brief.album_title.strip(), series))
        else:
            episodes = []
            for title in info.titles:
                cleaned = _strip_series(clean_track_title(title), series)
                if cleaned and cleaned not in episodes:
                    episodes.append(cleaned)

    if series and episodes == [series]:
        series = ""
    return series, "\n".join(episodes[:MAX_EPISODES])
