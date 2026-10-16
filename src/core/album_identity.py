"""
Wiedererkennung eines Albums ueber Umbenennen/Verschieben hinweg

Die App schreibt eine eigene Album-ID als Tag in die Audiodateien
(MP3: TXXX:TONUINO_ALBUM_ID). Daneben wird die von Picard/MusicBrainz
bekannte TXXX:MusicBrainz Album Id gelesen. Phase 1 nutzt das fuer die
Tracks auf der SD-Karte; die Bibliothek (Phase 3) erweitert das um weitere
Formate und den Abgleich ueber die Trackliste.
"""

import logging
import uuid
from typing import Iterable, Optional

log = logging.getLogger(__name__)

ALBUM_ID_DESC = "TONUINO_ALBUM_ID"
MB_ALBUM_ID_DESC = "MusicBrainz Album Id"
MB_RELEASE_GROUP_ID_DESC = "MusicBrainz Release Group Id"


def new_album_id() -> str:
    return uuid.uuid4().hex


def _open(filepath: str):
    from core.metadata import MetadataManager
    return MetadataManager._open_mp3(filepath)


def read_txxx(filepath: str, desc: str) -> Optional[str]:
    try:
        audio = _open(filepath)
    except Exception:
        return None
    if not audio.tags:
        return None
    frame = audio.tags.get(f"TXXX:{desc}")
    if frame and frame.text:
        value = str(frame.text[0]).strip()
        return value or None
    return None


def write_txxx(filepath: str, desc: str, value: str) -> bool:
    from mutagen.id3 import TXXX
    try:
        audio = _open(filepath)
        if audio.tags is None:
            audio.add_tags()
        audio.tags.setall(f"TXXX:{desc}", [TXXX(encoding=3, desc=desc, text=[value])])
        audio.save()
        return True
    except Exception as e:
        log.warning("Tag %s konnte nicht geschrieben werden (%s): %s", desc, filepath, e)
        return False


def read_album_id(filepaths: Iterable[str]) -> Optional[str]:
    """Erste gefundene App-Album-ID (nur die ersten Dateien werden gelesen)"""
    for i, path in enumerate(filepaths):
        if i >= 3:
            break
        value = read_txxx(path, ALBUM_ID_DESC)
        if value:
            return value
    return None


def read_mb_ids(filepath: str) -> dict:
    return {
        "release_id": read_txxx(filepath, MB_ALBUM_ID_DESC) or "",
        "release_group_id": read_txxx(filepath, MB_RELEASE_GROUP_ID_DESC) or "",
    }


def write_album_id(filepaths: Iterable[str], album_id: str) -> int:
    return sum(1 for path in filepaths if write_txxx(path, ALBUM_ID_DESC, album_id))
