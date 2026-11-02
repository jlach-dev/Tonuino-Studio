"""
Systemdateien der TonUINO-Firmware (Ordner mp3 und advert) auf der SD-Karte

Die Ansagen gehoeren zum GPL-Projekt TonUINO-TNG und werden nicht mitgeliefert,
sondern auf Wunsch aus dem passenden Release geladen. Jede Datei wird gegen die
Git-Pruefsumme des Releases geprueft. Geloescht wird nichts: Dateien, die nur auf
der Karte liegen, bleiben stehen.
"""

import hashlib
import json
import os
import re
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

from core.branding import APP_NAME
from core.fileutil import replace_with_retry

REPO = "tonuino/TonUINO-TNG"
API_URL = f"https://api.github.com/repos/{REPO}"
RAW_URL = f"https://raw.githubusercontent.com/{REPO}"
SYSTEM_FOLDERS = ("mp3", "advert")
# Name in der Auswahl -> Ordner im Release; aeltere Releases haben nur "sd-card"
LANGUAGES = {"Deutsch": "sd-card", "English": "sd-card-english", "Français": "sd-card-french"}
HTTP_TIMEOUT = 30
MAX_FILE_BYTES = 20 * 1024 * 1024
TEMP_PREFIX = ".tmp_system_"

# So meldet sich die Firmware beim Start: "V3.3.3 25.02.26" (Tag.Monat.Jahr)
FIRMWARE_LINE = re.compile(r"^V(\d+\.\d+\.\d+) (\d{2})\.(\d{2})\.(\d{2})$")
# Das Release enthaelt auch Namen wie 0508_quiz_game_buzzer_vol+.mp3
SAFE_NAME = re.compile(r'^[^./\\:*?"<>|][^/\\:*?"<>|]*\.mp3$', re.IGNORECASE)


class SystemFilesError(Exception):
    """Fehler beim Laden oder Pruefen der Systemdateien, Text fuer den Nutzer"""


@dataclass(frozen=True)
class FirmwareVersion:
    number: str  # "3.3.3"
    date: str  # "2026.02.25", wie im Release-Namen

    @property
    def label(self) -> str:
        return f"V{self.number} vom {self.date}"


@dataclass(frozen=True)
class SystemFile:
    folder: str  # "mp3" oder "advert"
    name: str
    sha: str  # Git-Pruefsumme aus dem Release
    size: int


@dataclass
class SyncPlan:
    missing: List[SystemFile] = field(default_factory=list)
    changed: List[SystemFile] = field(default_factory=list)
    same: int = 0
    only_on_card: List[str] = field(default_factory=list)


def parse_firmware_line(line: str) -> Optional[FirmwareVersion]:
    match = FIRMWARE_LINE.match(line.strip())
    if not match:
        return None
    number, day, month, year = match.groups()
    return FirmwareVersion(number, f"20{year}.{month}.{day}")


def detect_firmware(port: str, timeout: float = 8.0) -> Optional[FirmwareVersion]:
    """Oeffnet den Port und liest die Startmeldung der Box. Die meisten Arduinos starten
    dabei neu; kommt keine Versionszeile, ist das Ergebnis None."""
    try:
        import serial
    except ImportError as e:
        raise SystemFilesError("Das Paket 'pyserial' ist nicht installiert.") from e
    from core.tonuino_serial import TonuinoSerial
    try:
        with serial.Serial(port, TonuinoSerial.BAUDRATE, timeout=0.3) as connection:
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                version = parse_firmware_line(connection.readline().decode("utf-8", errors="replace"))
                if version is not None:
                    return version
    except serial.SerialException as e:
        raise SystemFilesError(f"{port} lässt sich nicht öffnen. Ist die Box noch mit der App "
                               f"verbunden? Dann bitte zuerst trennen. ({e})") from e
    return None


def match_release(tags: List[str], version: FirmwareVersion) -> Tuple[Optional[str], bool]:
    """Passendes Release: (Name, exakt). Ohne exakten Treffer das neueste derselben
    Versionsnummer, denn Datum im Programm und im Release koennen abweichen."""
    exact = f"V{version.number}_{version.date}"
    if exact in tags:
        return exact, True
    prefix = f"V{version.number}_"
    same_number = sorted(tag for tag in tags if tag.startswith(prefix))
    return (same_number[-1], False) if same_number else (None, False)


def _get(url: str, max_bytes: int = MAX_FILE_BYTES) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": APP_NAME, "Accept": "*/*"})
    with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT) as response:
        body = response.read(max_bytes + 1)
    if len(body) > max_bytes:
        raise SystemFilesError("Die Antwort von GitHub ist unerwartet groß.")
    return body


def fetch_releases() -> List[str]:
    """Release-Namen, neuestes zuerst"""
    data = json.loads(_get(f"{API_URL}/releases?per_page=100"))
    return [item["tag_name"] for item in data if not item.get("draft") and not item.get("prerelease")]


def fetch_manifest(tag: str, language_dir: str) -> List[SystemFile]:
    """Dateiliste des Releases fuer mp3 und advert"""
    data = json.loads(_get(f"{API_URL}/git/trees/{urllib.parse.quote(tag, safe='')}?recursive=1"))
    if data.get("truncated"):
        raise SystemFilesError("GitHub hat die Dateiliste des Releases nur unvollständig geliefert.")
    files = []
    for entry in data.get("tree", []):
        parts = entry.get("path", "").split("/")
        if (entry.get("type") == "blob" and len(parts) == 3 and parts[0] == language_dir
                and parts[1] in SYSTEM_FOLDERS and SAFE_NAME.match(parts[2])):
            files.append(SystemFile(parts[1], parts[2], entry["sha"], int(entry.get("size", 0))))
    if not files:
        raise SystemFilesError(f"Das Release {tag} enthält für diese Sprache keine Systemdateien.")
    return files


def git_blob_sha(data: bytes) -> str:
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


def _card_folder(root: Path, name: str) -> Path:
    """Vorhandener Systemordner in beliebiger Schreibweise, sonst der neue Pfad"""
    if root.is_dir():
        for item in root.iterdir():
            if item.is_dir() and item.name.lower() == name:
                return item
    return root / name


def _mp3_files(folder: Path) -> Dict[str, Path]:
    if not folder.is_dir():
        return {}
    return {item.name.lower(): item for item in folder.iterdir()
            if item.is_file() and item.suffix.lower() == ".mp3" and not item.name.startswith(".")}


def compare(root: Path, manifest: List[SystemFile]) -> SyncPlan:
    """Vergleicht die Karte per Pruefsumme mit dem Release, ohne etwas zu aendern"""
    plan = SyncPlan()
    for folder_name in SYSTEM_FOLDERS:
        on_card = _mp3_files(_card_folder(root, folder_name))
        expected = [f for f in manifest if f.folder == folder_name]
        for item in expected:
            existing = on_card.get(item.name.lower())
            if existing is None:
                plan.missing.append(item)
            elif existing.stat().st_size == item.size and git_blob_sha(existing.read_bytes()) == item.sha:
                plan.same += 1
            else:
                plan.changed.append(item)
        names = {f.name.lower() for f in expected}
        plan.only_on_card += [f"{folder_name}/{path.name}" for key, path in sorted(on_card.items())
                              if key not in names]
    return plan


def download(tag: str, language_dir: str, item: SystemFile) -> bytes:
    path = "/".join(urllib.parse.quote(part, safe="") for part in (tag, language_dir, item.folder, item.name))
    data = _get(f"{RAW_URL}/{path}")
    if git_blob_sha(data) != item.sha:
        raise SystemFilesError(f"{item.folder}/{item.name}: Die Prüfsumme stimmt nicht, die Datei wurde nicht geschrieben.")
    return data


def write_files(root: Path, tag: str, language_dir: str, files: List[SystemFile],
                progress: Optional[Callable[[int, int, str], None]] = None,
                cancelled: Optional[Callable[[], bool]] = None) -> int:
    """Laedt und schreibt die Dateien einzeln, jede erst unter einem Zwischennamen.
    Gibt die Zahl der geschriebenen Dateien zurueck, ein Abbruch laesst die
    fertigen stehen."""
    written = 0
    for index, item in enumerate(files, start=1):
        if cancelled is not None and cancelled():
            break
        if progress is not None:
            progress(index, len(files), f"{item.folder}/{item.name}")
        data = download(tag, language_dir, item)
        folder = _card_folder(root, item.folder)
        folder.mkdir(exist_ok=True)
        existing = _mp3_files(folder).get(item.name.lower())
        target = existing if existing is not None else folder / item.name
        temp = folder / f"{TEMP_PREFIX}{item.name}"
        try:
            with open(temp, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            replace_with_retry(temp, target)
        except OSError:
            temp.unlink(missing_ok=True)
            raise
        written += 1
    return written
