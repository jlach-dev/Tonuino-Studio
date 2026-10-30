"""
Stuecke erkennen per Audio-Fingerabdruck: Chromaprint (fpcalc) + AcoustID

- fpcalc (aus dem Chromaprint-Projekt, https://acoustid.org/chromaprint) berechnet
  lokal den Fingerabdruck einer Datei
- AcoustID (https://acoustid.org, kostenloser API-Key fuer nicht-kommerzielle
  Nutzung) ordnet dem Fingerabdruck MusicBrainz-Aufnahmen und Release-Groups zu;
  Limit: 3 Anfragen/s, aussagekraeftiger User-Agent
- Die erkannte Release-Group wird dann ueber MusicBrainz (core/tag_lookup.py)
  als Kandidat mit Trackliste geladen; die Zuordnung der Tracks erfolgt ueber
  die Recording-ID, nicht ueber Position/Dauer.
"""

import json
import logging
import os
import shutil
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional

from core.musicbrainz import USER_AGENT

log = logging.getLogger(__name__)

LOOKUP_URL = "https://api.acoustid.org/v2/lookup"
HTTP_TIMEOUT = 20
MIN_SCORE = 0.5

# Offizielles Chromaprint-Release (GitHub acoustid/chromaprint), feste Version
FPCALC_VERSION = "1.5.1"
FPCALC_DOWNLOADS = {
    "win32": f"https://github.com/acoustid/chromaprint/releases/download/v{FPCALC_VERSION}/"
             f"chromaprint-fpcalc-{FPCALC_VERSION}-windows-x86_64.zip",
    "darwin": f"https://github.com/acoustid/chromaprint/releases/download/v{FPCALC_VERSION}/"
              f"chromaprint-fpcalc-{FPCALC_VERSION}-macos-universal.tar.gz",
    "linux": f"https://github.com/acoustid/chromaprint/releases/download/v{FPCALC_VERSION}/"
             f"chromaprint-fpcalc-{FPCALC_VERSION}-linux-x86_64.tar.gz",
}
# Exakte Groessen und SHA-256 der offiziellen Archive (GitHub-Release v1.5.1,
# am 30.10.2026 einmal geladen und berechnet - GitHub selbst meldet fuer diese
# alten Assets keine Pruefsumme). Ein abweichender Download wird nicht installiert.
FPCALC_SIZES = {"win32": 1548264, "darwin": 2400481, "linux": 2752946}
FPCALC_SHA256 = {
    "win32": "36b478e16aa69f757f376645db0d436073a42c0097b6bb2677109e7835b59bbc",
    "darwin": "d4d8faff4b5f7c558d9be053da47804f9501eaa6c2f87906a9f040f38d61c860",
    "linux": "4d7433a7f778e5946d7225230681cbcd634e153316ecac87c538c33ac32387a5",
}
FPCALC_MAX_BYTES = 10 * 1024 * 1024

_last_request = 0.0
_lock = threading.Lock()


# --- fpcalc ---------------------------------------------------------------------------------

def fpcalc_name(platform: Optional[str] = None) -> str:
    import sys
    return "fpcalc.exe" if (platform or sys.platform) == "win32" else "fpcalc"


def find_fpcalc(configured: str = "", data_dir: str = "") -> Optional[str]:
    """Pfad zu fpcalc: Einstellung, sonst PATH, sonst <Datenordner>/tools"""
    if configured and os.path.isfile(configured):
        return configured
    found = shutil.which("fpcalc")
    if found:
        return found
    if data_dir:
        candidate = os.path.join(data_dir, "tools", fpcalc_name())
        if os.path.isfile(candidate):
            return candidate
    return None


def fingerprint(path: str, fpcalc: str) -> tuple:
    """(Dauer in Sekunden, Fingerabdruck) ueber `fpcalc -json`"""
    completed = subprocess.run([fpcalc, "-json", path], capture_output=True, text=True, encoding="utf-8",
                               errors="replace", timeout=120,
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if completed.returncode != 0:
        raise RuntimeError(f"fpcalc: {completed.stderr.strip() or completed.stdout.strip() or 'Fehler'}")
    data = json.loads(completed.stdout)
    return float(data.get("duration", 0.0)), str(data.get("fingerprint", ""))


def download_fpcalc(data_dir: str, platform: Optional[str] = None,
                    progress: Optional[Callable[[str], None]] = None) -> str:
    """Laedt das offizielle Chromaprint-Release herunter und entpackt fpcalc nach
    <Datenordner>/tools. Gibt den Pfad zurueck."""
    import io
    import sys
    import tarfile
    import zipfile
    import hashlib
    platform = platform or sys.platform
    if platform.startswith("linux"):
        platform = "linux"
    url = FPCALC_DOWNLOADS.get(platform)
    if url is None:
        raise RuntimeError(f"Für dieses System ({platform}) gibt es kein fertiges fpcalc - "
                           "bitte Chromaprint selbst installieren und den Pfad eintragen.")
    if progress:
        progress(f"Lade {url} …")
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=120) as response:
        payload = response.read(FPCALC_MAX_BYTES + 1)
    expected_size = FPCALC_SIZES.get(platform)
    if len(payload) > FPCALC_MAX_BYTES or (expected_size and len(payload) != expected_size):
        raise RuntimeError(f"Download unerwartet groß oder unvollständig ({len(payload)} Bytes) - "
                           "fpcalc wird nicht installiert.")
    expected_sha = FPCALC_SHA256.get(platform)
    if expected_sha and hashlib.sha256(payload).hexdigest() != expected_sha:
        raise RuntimeError("Prüfsumme des Downloads stimmt nicht - fpcalc wird nicht installiert.")
    target_dir = os.path.join(data_dir, "tools")
    os.makedirs(target_dir, exist_ok=True)
    name = fpcalc_name(platform)  # nach Zielplattform, nicht nach dem laufenden System
    target = os.path.join(target_dir, name)
    blob = None
    if url.endswith(".zip"):
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            for member_name in archive.namelist():
                if os.path.basename(member_name).lower() == name.lower():
                    blob = archive.read(member_name)
                    break
    else:
        with tarfile.open(fileobj=io.BytesIO(payload), mode="r:gz") as archive:
            for member in archive.getmembers():
                if os.path.basename(member.name) == "fpcalc" and member.isfile():
                    blob = archive.extractfile(member).read()
                    break
    if blob is None:
        raise RuntimeError("fpcalc nicht im heruntergeladenen Archiv gefunden")
    # Nur die eine Datei, an einen festen Pfad, erst temporaer und dann ersetzen
    tmp = target + ".tmp"
    with open(tmp, "wb") as handle:
        handle.write(blob)
    os.replace(tmp, target)
    if platform != "win32":
        os.chmod(target, 0o755)
    return target


# --- AcoustID -------------------------------------------------------------------------------

@dataclass
class Match:
    """Ein AcoustID-Treffer fuer eine Datei"""
    score: float
    recording_id: str
    title: str = ""
    artist: str = ""
    release_group_id: str = ""
    release_group_title: str = ""
    release_group_artist: str = ""


@dataclass
class FileResult:
    path: str
    duration: float = 0.0
    matches: List[Match] = field(default_factory=list)
    error: str = ""

    @property
    def best(self) -> Optional[Match]:
        return self.matches[0] if self.matches else None


@dataclass
class RecognitionResult:
    files: List[FileResult] = field(default_factory=list)
    release_group_id: str = ""  # gemeinsame Release-Group der Mehrheit
    release_group_title: str = ""
    release_group_artist: str = ""
    agreeing: int = 0  # Dateien, die zur gemeinsamen Release-Group passen

    @property
    def recognized(self) -> int:
        return sum(1 for f in self.files if f.best is not None)

    def recording_ids(self) -> Dict[str, str]:
        """Dateipfad -> Recording-ID (beste Zuordnung innerhalb der gemeinsamen Release-Group)"""
        result = {}
        for item in self.files:
            match = next((m for m in item.matches if m.release_group_id == self.release_group_id), item.best) \
                if self.release_group_id else item.best
            if match is not None:
                result[item.path] = match.recording_id
        return result


def _throttle():
    global _last_request
    with _lock:
        wait = 0.34 - (time.monotonic() - _last_request)
        if wait > 0:
            time.sleep(wait)
        _last_request = time.monotonic()


def lookup(api_key: str, duration: float, fp: str) -> List[Match]:
    """Treffer von AcoustID (nach Score sortiert) mit Aufnahme und Release-Groups"""
    _throttle()
    params = urllib.parse.urlencode({"client": api_key, "duration": int(round(duration)), "fingerprint": fp,
                                     "meta": "recordings releasegroups compress", "format": "json"})
    request = urllib.request.Request(LOOKUP_URL, data=params.encode("ascii"),
                                     headers={"User-Agent": USER_AGENT,
                                              "Content-Type": "application/x-www-form-urlencoded"})
    try:
        with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT) as response:
            data = json.loads(response.read())
    except urllib.error.HTTPError as e:
        # AcoustID antwortet bei Client-Fehlern mit 400 und einer JSON-Fehlermeldung im Body
        try:
            data = json.loads(e.read())
        except (ValueError, OSError):
            raise RuntimeError(f"AcoustID: HTTP {e.code} {e.reason}") from e
    if data.get("status") != "ok":
        raise RuntimeError(error_text(data))
    return parse_lookup(data)


ERROR_HINTS = {
    "invalid API key": "ungültiger API-Key – es wird der Application-Key von "
                       "https://acoustid.org/applications benötigt (nicht der User-Key)",
    "invalid fingerprint": "ungültiger Fingerabdruck (fpcalc-Ausgabe)",
    "invalid duration": "ungültige Dauer",
}


def error_text(data: dict) -> str:
    """Deutsche Fehlermeldung aus einer AcoustID-Fehlerantwort"""
    message = str((data.get("error") or {}).get("message") or data.get("status") or "Fehler")
    return "AcoustID: " + ERROR_HINTS.get(message, message)


def parse_lookup(data: dict) -> List[Match]:
    matches: List[Match] = []
    for result in data.get("results", []):
        score = float(result.get("score", 0.0))
        for recording in result.get("recordings", []) or []:
            artists = recording.get("artists") or []
            artist = ", ".join(a.get("name", "") for a in artists if a.get("name"))
            groups = recording.get("releasegroups") or []
            if not groups:
                matches.append(Match(score, recording.get("id", ""), recording.get("title", ""), artist))
            for group in groups:
                group_artists = group.get("artists") or []
                matches.append(Match(score, recording.get("id", ""), recording.get("title", ""), artist,
                                     group.get("id", ""), group.get("title", ""),
                                     ", ".join(a.get("name", "") for a in group_artists if a.get("name"))))
    matches.sort(key=lambda m: -m.score)
    return [m for m in matches if m.score >= MIN_SCORE]


def group_results(files: List[FileResult]) -> RecognitionResult:
    """Gemeinsame Release-Group der Dateien bestimmen: je Datei zaehlt jede
    Release-Group mit ihrem besten Score; die Gruppe mit der hoechsten Summe
    gewinnt (Mehrheit, nach Score gewichtet)."""
    result = RecognitionResult(files=files)
    weights: Dict[str, float] = {}
    titles: Dict[str, tuple] = {}
    for item in files:
        seen = {}
        for match in item.matches:
            if match.release_group_id and match.release_group_id not in seen:
                seen[match.release_group_id] = match.score
                titles.setdefault(match.release_group_id, (match.release_group_title, match.release_group_artist))
        for group_id, score in seen.items():
            weights[group_id] = weights.get(group_id, 0.0) + score
    if weights:
        best = max(weights.items(), key=lambda kv: kv[1])[0]
        result.release_group_id = best
        result.release_group_title, result.release_group_artist = titles.get(best, ("", ""))
        result.agreeing = sum(1 for f in files if any(m.release_group_id == best for m in f.matches))
    return result


def recognize_files(paths: List[str], api_key: str, fpcalc: str,
                    progress: Optional[Callable[[int, int, str], None]] = None,
                    cancelled: Callable[[], bool] = lambda: False) -> RecognitionResult:
    """Fingerabdruck + AcoustID je Datei, danach Gruppierung"""
    files: List[FileResult] = []
    for index, path in enumerate(paths, start=1):
        if cancelled():
            break
        if progress:
            progress(index, len(paths), os.path.basename(path))
        item = FileResult(path)
        try:
            item.duration, fp = fingerprint(path, fpcalc)
            item.matches = lookup(api_key, item.duration, fp)
        except Exception as e:
            log.warning("Erkennung fehlgeschlagen (%s): %s", path, e)
            item.error = str(e)
        files.append(item)
    return group_results(files)
