"""
Ein Album der Bibliothek auf die SD-Karte eines ESPuino bringen.

Anders als beim TonUINO behaelt das Album seinen Namen als Ordner und die
Dateien ihre Namen ("/Conni geht verloren/02 - Teil 01.mp3"). Nur wenn die
Dateinamen alphabetisch nicht in Abspielreihenfolge stehen, bekommen sie eine
Nummer davor ("01 ", "02 " ...), denn der ESPuino spielt einen Ordner sortiert.

Zwei Wege, je nachdem, ob die SD-Karte gerade im PC steckt:
  - SD-Karte im Kartenleser (Einstellung espuino/sd_root): Dateien kopieren
  - sonst ueber das WLAN: je Datei POST /explorer?path=<Ziel> mit dem rohen
    Inhalt (wie die Weboberflaeche des ESPuino; fehlende Ordner legt er selbst an,
    er nimmt immer nur ein Hochladen gleichzeitig an)

Eigene Datei des Forks (siehe core/branding.py).
"""

import json
import os
import re
import shutil
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, List, Optional, Set

from core.espuino_device import DeviceError, normalize_host

INVALID_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
MAX_NAME = 120  # Zeichen je Ordner-/Dateiname; der ESPuino speichert Pfade bis 255 Byte
UPLOAD_TIMEOUT = 120  # Sekunden je Datei; der ESPuino schreibt grob 0,3 bis 1 MB/s
CHUNK = 64 * 1024

Progress = Callable[[int, int, str], None]  # (Bytes fertig, Bytes gesamt, aktuelle Datei)


@dataclass
class TransferItem:
    source: str  # Datei auf dem PC
    target: str  # Pfad auf der SD-Karte, "/Ordner/Datei.mp3"
    size: int = 0


@dataclass
class TransferPlan:
    folder: str  # "/Ordner" auf der SD-Karte
    items: List[TransferItem] = field(default_factory=list)

    @property
    def total(self) -> int:
        return sum(i.size for i in self.items)


@dataclass
class TransferResult:
    copied: int = 0
    skipped: int = 0  # gab es am Ziel schon
    bytes: int = 0
    cancelled: bool = False


def safe_name(name: str, fallback: str = "Album") -> str:
    """Name fuer FAT32/exFAT: verbotene Zeichen raus, keine Punkte/Leerzeichen am Ende"""
    name = INVALID_CHARS.sub(" ", name or "")
    name = re.sub(r"\s+", " ", name).strip().rstrip(". ")
    if len(name) > MAX_NAME:
        stem, ext = os.path.splitext(name)
        name = stem[:MAX_NAME - len(ext)].rstrip(". ") + ext
    return name or fallback


def normalize_folder(folder: str) -> str:
    parts = [safe_name(p, "") for p in folder.replace("\\", "/").split("/")]
    parts = [p for p in parts if p and p not in (".", "..")]
    if not parts:
        raise DeviceError("Bitte einen Zielordner angeben, nicht die ganze SD-Karte.")
    return "/" + "/".join(parts)


def plan_album(tracks: List[str], folder: str) -> TransferPlan:
    """Zielnamen fuer die Tracks (in Abspielreihenfolge) im Ordner folder"""
    folder = normalize_folder(folder)
    names = [safe_name(os.path.basename(t), f"{i:02d}") for i, t in enumerate(tracks, 1)]
    in_order = [n.casefold() for n in names] == sorted(n.casefold() for n in names)
    if not in_order or len({n.casefold() for n in names}) != len(names):
        width = max(2, len(str(len(names))))
        names = [f"{i:0{width}d} {n}" for i, n in enumerate(names, 1)]
    plan = TransferPlan(folder)
    for track, name in zip(tracks, names):
        try:
            size = os.path.getsize(track)
        except OSError:
            size = 0
        plan.items.append(TransferItem(track, f"{folder}/{name}", size))
    return plan


def _local(sd_root: str, sd_path: str) -> Path:
    return Path(sd_root).joinpath(*[p for p in sd_path.split("/") if p])


def copy_to_sd(plan: TransferPlan, sd_root: str, progress: Optional[Progress] = None,
               cancelled: Callable[[], bool] = lambda: False) -> TransferResult:
    """Auf die SD-Karte im Kartenleser kopieren. Vorhandene Dateien mit gleicher
    Groesse werden uebersprungen (Neustart nach Abbruch)."""
    if not sd_root or not os.path.isdir(sd_root):
        raise DeviceError("Die SD-Karte des ESPuino steckt nicht im PC.")
    result = TransferResult()
    total = plan.total
    for item in plan.items:
        if cancelled():
            result.cancelled = True
            break
        target = _local(sd_root, item.target)
        if progress:
            progress(result.bytes, total, os.path.basename(item.target))
        if target.is_file() and target.stat().st_size == item.size:
            result.skipped += 1
            result.bytes += item.size
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        partial = target.with_name(target.name + ".part")
        try:
            shutil.copyfile(item.source, partial)
            os.replace(partial, target)
        except OSError as exc:
            partial.unlink(missing_ok=True)
            raise DeviceError(f"Kopieren von „{os.path.basename(item.source)}“ fehlgeschlagen: {exc}") from None
        result.copied += 1
        result.bytes += item.size
    if progress:
        progress(result.bytes, total, "")
    return result


def list_folder(host: str, sd_path: str) -> Set[str]:
    """Dateinamen in einem Ordner der SD-Karte des ESPuino (leer, wenn es ihn nicht gibt)"""
    url = f"http://{normalize_host(host)}/explorer?path={urllib.parse.quote(sd_path)}"
    try:
        with urllib.request.urlopen(url, timeout=15) as response:
            entries = json.loads(response.read().decode("utf-8-sig"))
    except urllib.error.HTTPError:
        return set()
    except (urllib.error.URLError, OSError) as exc:
        raise DeviceError(f"ESPuino unter {normalize_host(host)} nicht erreichbar "
                          f"({getattr(exc, 'reason', exc)}).") from None
    except (UnicodeDecodeError, json.JSONDecodeError):
        return set()
    if not isinstance(entries, list):
        return set()
    return {str(e.get("name")) for e in entries if isinstance(e, dict) and not e.get("dir") and e.get("name")}


class _ProgressReader:
    """Datei-Inhalt fuer urllib in Stuecken, meldet den Fortschritt"""

    def __init__(self, path: str, on_read: Callable[[int], None], cancelled: Callable[[], bool]):
        self._file = open(path, "rb")
        self._on_read = on_read
        self._cancelled = cancelled

    def read(self, size: int = -1) -> bytes:
        if self._cancelled():
            raise _Cancelled()
        data = self._file.read(CHUNK if size is None or size < 0 else min(size, CHUNK))
        if data:
            self._on_read(len(data))
        return data

    def close(self):
        self._file.close()


class _Cancelled(Exception):
    pass


def _delete_partial(host: str, sd_path: str) -> None:
    """Halb hochgeladene Datei entfernen, sonst gaelte sie beim naechsten Mal als
    vorhanden (der ESPuino schreibt direkt unter dem Zielnamen). Nur nach eigenem
    Abbruch oder Fehler, immer nur genau diese eine Datei."""
    url = f"http://{host}/explorer?path={urllib.parse.quote(sd_path)}"
    try:
        with urllib.request.urlopen(urllib.request.Request(url, method="DELETE"), timeout=15) as response:
            response.read()
    except (urllib.error.URLError, OSError):
        pass


def upload_to_device(plan: TransferPlan, host: str, progress: Optional[Progress] = None,
                     cancelled: Callable[[], bool] = lambda: False) -> TransferResult:
    """Ueber das WLAN hochladen. Dateien, die es im Zielordner schon gibt, werden
    uebersprungen (der ESPuino nennt keine Groessen, nur Namen)."""
    host = normalize_host(host)
    result = TransferResult()
    total = plan.total
    existing: dict = {}
    for item in plan.items:
        if cancelled():
            result.cancelled = True
            break
        folder, _, name = item.target.rpartition("/")
        if folder not in existing:
            existing[folder] = list_folder(host, folder or "/")
        if name in existing[folder]:
            result.skipped += 1
            result.bytes += item.size
            continue
        done_before = result.bytes
        sent = [0]

        def on_read(n, name=name, done_before=done_before):
            sent[0] += n
            if progress:
                progress(done_before + sent[0], total, name)

        reader = _ProgressReader(item.source, on_read, cancelled)
        url = f"http://{host}/explorer?path={urllib.parse.quote(item.target)}"
        request = urllib.request.Request(url, data=reader, method="POST")
        request.add_header("Content-Type", "application/octet-stream")
        request.add_header("Content-Length", str(item.size))
        try:
            with urllib.request.urlopen(request, timeout=UPLOAD_TIMEOUT) as response:
                response.read()
        except _Cancelled:
            _delete_partial(host, item.target)
            result.cancelled = True
            break
        except urllib.error.HTTPError as exc:
            _delete_partial(host, item.target)
            raise DeviceError(f"Der ESPuino hat „{name}“ abgelehnt ({exc.code}). "
                              "Ist die SD-Karte voll?") from None
        except (urllib.error.URLError, OSError) as exc:
            _delete_partial(host, item.target)
            raise DeviceError(f"Hochladen von „{name}“ abgebrochen ({getattr(exc, 'reason', exc)}).") from None
        finally:
            reader.close()
        result.copied += 1
        result.bytes += item.size
    if progress:
        progress(result.bytes, total, "")
    return result
