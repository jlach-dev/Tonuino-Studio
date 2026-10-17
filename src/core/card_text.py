"""
Kartentext aus den Tags ableiten: Hoerspielreihe (klein) und Folgentitel

Beispiele:
  TALB "Folge 39: Fall 77: Die verschleppte Schleppe/Fall 78: Der gefiederte Vergessling",
  TPE1 "Die Fuchsbande"
      -> Reihe "Die Fuchsbande · Folge 39"
         Titel "Fall 77: Die verschleppte Schleppe" / "Fall 78: Der gefiederte Vergessling"
  zwei Folgen auf einer Karte (verschiedene TALB je Track):
      "Bibi Blocksberg - Folge 1: Hexen gibt es doch", "Bibi Blocksberg - Folge 2: ..."
      -> Reihe "Bibi Blocksberg", Titel "Folge 1: Hexen gibt es doch" / "Folge 2: ..."
"""

import re
from typing import List, Optional, Tuple

from core.cover_research import CoverBrief, FolderInfo, clean_track_title

MAX_EPISODES = 4

_EPISODE_NO = re.compile(r"^\s*((?:folge|episode|teil|band|vol\.?|nr\.?)\s*\d+)\s*[:.\-–]\s*", re.IGNORECASE)
_SPLIT = re.compile(r"\s*/\s*")


def _strip_series(text: str, series: str) -> str:
    """'Die Fuchsbande - Folge 39' -> 'Folge 39' (Reihe steht separat)"""
    if series and text.lower().startswith(series.lower()):
        rest = text[len(series):].lstrip(" -–:·|")
        return rest or text
    return text


def _split_episodes(album: str) -> List[str]:
    """Doppelfolgen in einem Album-Tag ('Fall 77: …/Fall 78: …') trennen -
    nur wenn alle Teile wie eigene Titel aussehen (z.B. nicht 'AC/DC')"""
    parts = [p.strip() for p in _SPLIT.split(album) if p.strip()]
    if len(parts) > 1 and all(len(p) >= 8 for p in parts):
        return parts
    return [album.strip()]


def derive_card_text(info: FolderInfo, brief: Optional[CoverBrief] = None) -> Tuple[str, str]:
    """(Reihe, Titel mit einer Zeile je Folge)"""
    series = (info.album_artist or info.artist or (brief.series if brief else "")).strip()
    albums = [a.strip() for a in info.albums if a.strip()] or ([info.album.strip()] if info.album.strip() else [])

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
