"""
Datenordner der Bibliothek an einen anderen Ort umziehen

Kopiert werden nur die Eintraege der App, denn im Ordner koennen auch fremde
Dateien liegen. Alles kommt erst in einen Zwischenordner am Ziel und wird dort
geprueft. Der alte Ordner bleibt unveraendert stehen.
"""

import filecmp
import os
import shutil
from pathlib import Path
from typing import Callable, List, Optional, Tuple

from core.fileutil import replace_with_retry

DATA_ENTRIES = ("albums", "series", "tools", "library_thumbs", "library_index.json")
STAGING_NAME = ".tmp_datenordner"
# Legt Windows oder Google Drive selbst an, gehoert nicht zu den Daten
SKIP_FILES = {"desktop.ini"}


class DataFolderError(Exception):
    """Der Umzug ist nicht moeglich, Text fuer den Nutzer"""


def _normal(path) -> str:
    return os.path.normcase(os.path.abspath(str(path)))


def _is_inside(child: str, parent: str) -> bool:
    try:
        return os.path.commonpath([child, parent]) == parent
    except ValueError:  # anderes Laufwerk
        return False


def has_data(folder) -> bool:
    return any((Path(folder) / entry).exists() for entry in DATA_ENTRIES)


def validate_target(old: str, new: str) -> None:
    if not old:
        return
    if _normal(old) == _normal(new):
        raise DataFolderError("Das ist bereits der aktuelle Datenordner.")
    if _is_inside(_normal(new), _normal(old)):
        raise DataFolderError("Der neue Ordner darf nicht im bisherigen Datenordner liegen.")


def _data_files(old: Path) -> List[Tuple[Path, Path]]:
    """(Quelle, Pfad relativ zum Datenordner) aller zu kopierenden Dateien"""
    files = []
    for entry in DATA_ENTRIES:
        source = old / entry
        if source.is_file():
            files.append((source, Path(entry)))
        elif source.is_dir():
            for folder, _dirs, names in os.walk(source):
                for name in names:
                    if name.lower() not in SKIP_FILES:
                        path = Path(folder) / name
                        files.append((path, path.relative_to(old)))
    return files


def measure(old: str) -> Tuple[int, int]:
    """Zahl und Gesamtgroesse der Dateien in Bytes"""
    files = _data_files(Path(old))
    return len(files), sum(source.stat().st_size for source, _ in files)


def _copy_verified(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, target)
    try:
        shutil.copystat(source, target)
    except OSError:
        pass  # Zeitstempel sind nicht wichtig, manche Netzlaufwerke lehnen sie ab
    if not filecmp.cmp(source, target, shallow=False):
        raise DataFolderError(f"{target.name}: Die Kopie weicht vom Original ab.")


def copy_data(old: str, new: str, progress: Optional[Callable[[int, int, str], None]] = None,
              cancelled: Optional[Callable[[], bool]] = None) -> bool:
    """Kopiert die Daten nach new. False: abgebrochen. Bei Abbruch oder Fehler
    bleibt am Ziel nichts zurueck, was die App fuer Daten halten koennte."""
    validate_target(old, new)
    old_path, new_path = Path(old), Path(new)
    if has_data(new_path):
        raise DataFolderError("Im neuen Ordner liegen schon Daten der App.")
    files = _data_files(old_path)
    new_path.mkdir(parents=True, exist_ok=True)
    needed = sum(source.stat().st_size for source, _ in files)
    if shutil.disk_usage(new_path).free < needed:
        raise DataFolderError(f"Am neuen Ort ist zu wenig Platz, gebraucht werden {needed // (1024 * 1024) + 1} MB.")

    staging = new_path / STAGING_NAME
    placed = []
    try:
        shutil.rmtree(staging, ignore_errors=True)
        staging.mkdir()
        for index, (source, relative) in enumerate(files, start=1):
            if cancelled is not None and cancelled():
                return False
            if progress is not None:
                progress(index, len(files), str(relative))
            _copy_verified(source, staging / relative)
        for entry in DATA_ENTRIES:
            if (staging / entry).exists():
                replace_with_retry(staging / entry, new_path / entry)
                placed.append(new_path / entry)
        return True
    except BaseException:
        # Das Ziel war vorher frei von Daten, alles hier ist also unsere eigene Kopie
        for path in placed:
            if path.is_dir():
                shutil.rmtree(path, ignore_errors=True)
            else:
                path.unlink(missing_ok=True)
        raise
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def moved_path(path: str, old: str, new: str) -> str:
    """Gespeicherten Pfad (z. B. fpcalc) umschreiben, wenn er im alten Ordner liegt
    und die Datei auch im neuen vorhanden ist"""
    if not path or not old or not _is_inside(_normal(path), _normal(old)):
        return path
    candidate = Path(new) / os.path.relpath(_normal(path), _normal(old))
    return str(candidate) if candidate.exists() else path
