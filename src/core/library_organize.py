"""
Dateien der Bibliothek nach Tags einsortieren ("Struktur in die Sammlung bringen")

Ziel je Datei: <Quellordner>/<Interpret>/<Album>/<NN - Titel>.<ext>
(Schema anpassbar). Erst eine Vorschau alt -> neu, dann Verschieben;
vorhandene Dateien werden nie ueberschrieben. Die Album-ID im Tag haelt die
Zuordnung zum Archiv auch nach dem Verschieben.
"""

import os
import re
import shutil
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional

from core.library import TrackTags, read_track_tags

DEFAULT_SCHEME = "{albumartist}/{album}/{track:02d} - {title}"
SCHEME_FIELDS = ("albumartist", "artist", "album", "title", "track", "year")

_INVALID = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def safe_name(text: str, fallback: str = "Unbekannt") -> str:
    """Datei-/Ordnername ohne verbotene Zeichen, ohne Punkte/Leerzeichen am Ende"""
    cleaned = _INVALID.sub("_", (text or "").strip()).rstrip(". ").strip()
    return cleaned[:120] or fallback


@dataclass
class MovePlan:
    source: str
    target: str
    tags: TrackTags
    status: str = "ok"  # ok | unchanged | missing_tags | conflict
    note: str = ""

    @property
    def will_move(self) -> bool:
        return self.status == "ok"


@dataclass
class OrganizeResult:
    moved: int = 0
    skipped: int = 0
    errors: List[str] = field(default_factory=list)
    removed_dirs: int = 0


def target_for(tags: TrackTags, root: str, ext: str, scheme: str = DEFAULT_SCHEME) -> Optional[str]:
    """Zielpfad nach Schema; None, wenn Album oder Titel fehlen"""
    if not (tags.album and tags.title):
        return None
    values = {
        "albumartist": safe_name(tags.album_artist or tags.artist, "Unbekannter Interpret"),
        "artist": safe_name(tags.artist or tags.album_artist, "Unbekannter Interpret"),
        "album": safe_name(tags.album, "Unbekanntes Album"),
        "title": safe_name(tags.title, "Unbekannter Titel"),
        "track": int(tags.track_number or 0),
        "year": safe_name(getattr(tags, "year", "") or "", ""),
    }
    try:
        relative = scheme.format(**values)
    except (KeyError, ValueError, IndexError):
        relative = DEFAULT_SCHEME.format(**values)
    parts = [safe_name(p) for p in relative.replace("\\", "/").split("/") if p.strip()]
    if not parts:
        return None
    return os.path.join(root, *parts) + ext.lower()


def plan_moves(paths: List[str], root: str, scheme: str = DEFAULT_SCHEME) -> List[MovePlan]:
    """Vorschau: fuer jede Datei den Zielpfad bestimmen und Konflikte markieren"""
    plans: List[MovePlan] = []
    taken: Dict[str, str] = {}
    for path in paths:
        tags = read_track_tags(path)
        ext = os.path.splitext(path)[1]
        target = target_for(tags, root, ext, scheme)
        plan = MovePlan(path, target or "", tags)
        if target is None:
            plan.status, plan.note = "missing_tags", "Album oder Titel fehlt im Tag"
        elif os.path.normcase(os.path.normpath(target)) == os.path.normcase(os.path.normpath(path)):
            plan.status, plan.note = "unchanged", "liegt schon richtig"
        elif os.path.exists(target) or os.path.normcase(target) in taken:
            plan.status, plan.note = "conflict", "Zieldatei existiert bereits"
        else:
            taken[os.path.normcase(target)] = path
        plans.append(plan)
    return plans


def execute_moves(plans: List[MovePlan], remove_empty_dirs: bool = True,
                  progress: Optional[Callable[[int, int, str], None]] = None) -> OrganizeResult:
    """Verschiebt alle Plaene mit Status ok; leere Ursprungsordner werden entfernt"""
    result = OrganizeResult()
    origins = set()
    todo = [p for p in plans if p.will_move]
    for index, plan in enumerate(todo, start=1):
        if progress:
            progress(index, len(todo), os.path.basename(plan.source))
        try:
            if os.path.exists(plan.target):
                result.skipped += 1
                continue
            os.makedirs(os.path.dirname(plan.target), exist_ok=True)
            shutil.move(plan.source, plan.target)
            result.moved += 1
            origins.add(os.path.dirname(plan.source))
        except OSError as e:
            result.errors.append(f"{os.path.basename(plan.source)}: {e}")
    if remove_empty_dirs:
        for folder in sorted(origins, key=len, reverse=True):
            try:
                leftovers = [n for n in os.listdir(folder) if not n.lower() in ("thumbs.db", "desktop.ini")]
                if not leftovers or all(n.startswith(".") for n in leftovers):
                    for name in os.listdir(folder):
                        os.remove(os.path.join(folder, name))
                    os.rmdir(folder)
                    result.removed_dirs += 1
            except OSError:
                pass
    return result
