"""
Karten direkt vom ESPuino lesen und zuweisen, ohne Mediahub.

Der ESPuino (Firmware 3.x, src/Web.cpp) hat eine REST-Schnittstelle, die auch
seine eigene Weboberflaeche benutzt, ohne Anmeldung im WLAN:
    GET    /info                 Firmware, SD-Karte, WLAN
    GET    /rfid                 alle Zuweisungen: [{"id", "fileOrUrl", "playMode",
                                 "lastPlayPos", "trackLastPlayed"}] bzw. {"id", "modId"}
                                 bei Funktionskarten
    POST   /rfid                 {"id", "fileOrUrl", "playMode"} speichert eine Zuweisung
    WS     /ws                   meldet jede aufgelegte Karte als {"rfidId": "..."}
"fileOrUrl" ist ein Pfad auf der SD-Karte des ESPuino ("/Ordner" oder
"/Ordner/titel.mp3") oder eine Webradio-Adresse.

Die Dateien selbst liest Studio nicht ueber das WLAN (zu langsam), sondern aus
einem Ordner auf dem PC: der SD-Karte im Kartenleser oder einer Kopie davon.

Karten bekommen dieselbe Album-ID wie im Mediahub-Modus (mediahub<Kartennummer>),
ein Kartencover gehoert also zur physischen Karte, egal woher sie gelesen wurde.

Eigene Datei des Forks (siehe core/branding.py).
"""

import codecs
import json
import logging
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Dict, List, Optional, Set, Tuple

from core.mediahub_cards import KIND_FILES, Device, MediahubCard, MediahubData

log = logging.getLogger(__name__)

SETTINGS_SOURCE = "espuino/source"
SETTINGS_HOST = "espuino/host"
SETTINGS_SD_ROOT = "espuino/sd_root"
SETTINGS_DEVICES = "espuino/devices"  # JSON-Liste aller eingerichteten ESPuinos
SETTINGS_ASK_ON_START = "espuino/ask_on_start"
SETTINGS_USE_ACR122U = "espuino/use_acr122u"  # USB-Leser zum Auslesen (Standard aus)
SOURCE_DEVICE = "device"
SOURCE_MEDIAHUB = "mediahub"
SOURCES = (SOURCE_DEVICE, SOURCE_MEDIAHUB)

TIMEOUT = 6  # Sekunden; der ESPuino antwortet im WLAN meist in unter einer Sekunde
MAX_FILE_OR_URL = 255  # Puffer der Firmware: 275 Zeichen fuer die ganze Zeile

AUDIO_EXTENSIONS = {".mp3", ".m4a", ".m4b", ".aac", ".ogg", ".opus", ".flac", ".wav", ".wma"}

# Abspielmodi, die Unterordner einbeziehen (values.h der Firmware)
RECURSIVE_MODES = {13, 14, 15, 16, 17}
WEBSTREAM = 8
LOCAL_M3U = 11
MEDIAHUB_MODE = 18  # Karte wird vom Mediahub verwaltet

# Abspielmodi, die man in Studio fuer eine Karte waehlen kann (Ordner oder Datei)
ASSIGNABLE_MODES = (3, 5, 6, 16, 15, 17, 4, 7, 9, 12, 13, 14, 1, 2)
FILE_MODES = {1, 2}  # Modi fuer eine einzelne Datei


class DeviceError(Exception):
    """ESPuino nicht erreichbar oder unerwartete Antwort"""


def normalize_host(text: str) -> str:
    """'http://192.168.178.124/' -> '192.168.178.124'; Port bleibt erhalten"""
    host = (text or "").strip()
    host = re.sub(r"^[a-zA-Z]+://", "", host)
    host = host.split("/", 1)[0].strip()
    if not host or not re.fullmatch(r"[A-Za-z0-9.\-]+(:\d{1,5})?|\[[0-9a-fA-F:]+\](:\d{1,5})?", host):
        raise DeviceError(f"Ungültige Adresse: {text!r}")
    return host


def card_id_from_uid(uid_hex: str) -> str:
    """Kartennummer, wie der ESPuino sie bildet, aus der UID einer Karte (hex, etwa
    vom ACR122U): die ersten vier Bytes je dreistellig dezimal, "5C429102" ->
    "092066145002" (Firmware: RfidMfrc522.cpp, cardIdSize 4). "" bei kuerzerer UID."""
    try:
        data = bytes.fromhex(uid_hex.replace(" ", "").replace(":", ""))
    except ValueError:
        return ""
    return "".join(f"{b:03d}" for b in data[:4]) if len(data) >= 4 else ""


def _url(host: str, path: str) -> str:
    return f"http://{normalize_host(host)}{path}"


def ws_url(host: str) -> str:
    return f"ws://{normalize_host(host)}/ws"


def _request(host: str, path: str, data: Optional[dict] = None, timeout: float = TIMEOUT):
    body = None if data is None else json.dumps(data).encode("utf-8")
    request = urllib.request.Request(_url(host, path), data=body, method="GET" if data is None else "POST")
    if body is not None:
        request.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read()
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace").strip()[:200]
        raise DeviceError(f"Der ESPuino meldet einen Fehler ({exc.code}): {detail or exc.reason}") from None
    except (urllib.error.URLError, OSError) as exc:
        reason = getattr(exc, "reason", exc)
        raise DeviceError(f"ESPuino unter {normalize_host(host)} nicht erreichbar ({reason}). "
                          "Ist er eingeschaltet und im selben WLAN?") from None
    try:
        return json.loads(raw.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise DeviceError(f"Unerwartete Antwort von {normalize_host(host)}{path} – ist das ein ESPuino?") from None


def fetch_info(host: str, timeout: float = TIMEOUT) -> dict:
    info = _request(host, "/info", timeout=timeout)
    if not isinstance(info, dict) or "software" not in info:
        raise DeviceError(f"{normalize_host(host)} antwortet, ist aber offenbar kein ESPuino.")
    return info


def firmware_label(info: dict) -> str:
    software = info.get("software") or {}
    version = str(software.get("version") or "").replace("Software-revision:", "").strip()
    branch = str(software.get("branch") or "").strip()
    return f"{version} ({branch})" if version and branch else version or "unbekannt"


def fetch_assignments(host: str) -> List[dict]:
    entries = _request(host, "/rfid")
    if not isinstance(entries, list):
        raise DeviceError("Unerwartete Antwort auf /rfid – keine Liste von Karten.")
    return [e for e in entries if isinstance(e, dict) and str(e.get("id", "")).strip()]


def assign_card(host: str, card_id: str, file_or_url: str, play_mode: int) -> dict:
    """Zuweisung auf dem ESPuino speichern (wie in seiner Weboberflaeche)"""
    card_id = str(card_id).strip()
    if not card_id.isdigit():
        raise DeviceError(f"Ungültige Kartennummer: {card_id!r}")
    if not file_or_url or len(file_or_url.encode("utf-8")) > MAX_FILE_OR_URL:
        raise DeviceError(f"Der Pfad ist leer oder länger als {MAX_FILE_OR_URL} Byte.")
    if int(play_mode) <= 0:
        raise DeviceError(f"Ungültiger Abspielmodus: {play_mode!r}")
    result = _request(host, "/rfid", {"id": card_id, "fileOrUrl": file_or_url, "playMode": int(play_mode)})
    if not isinstance(result, dict) or str(result.get("id")) != card_id:
        raise DeviceError("Der ESPuino hat die Zuweisung nicht bestätigt.")
    return result


def unassign_card(host: str, card_id: str) -> bool:
    """Zuweisung einer Karte auf dem ESPuino entfernen (DELETE /rfid?id=).
    False, wenn er die Karte gar nicht kannte. Seine backup.txt schreibt er dabei
    nicht neu - das tut er erst bei der naechsten Zuweisung."""
    card_id = str(card_id).strip()
    if not card_id.isdigit():
        raise DeviceError(f"Ungültige Kartennummer: {card_id!r}")
    request = urllib.request.Request(_url(host, "/rfid?id=" + card_id), method="DELETE")
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            response.read()
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return False
        detail = exc.read().decode("utf-8", "replace").strip()[:200]
        raise DeviceError(f"Der ESPuino meldet einen Fehler ({exc.code}): {detail or exc.reason}") from None
    except (urllib.error.URLError, OSError) as exc:
        raise DeviceError(f"ESPuino unter {normalize_host(host)} nicht erreichbar "
                          f"({getattr(exc, 'reason', exc)}).") from None
    return True


def sd_path_from_local(sd_root: str, local_path: str) -> str:
    """Ordner oder Datei auf dem PC -> Pfad auf der SD-Karte ('/Ordner/...')"""
    root = Path(sd_root).resolve()
    path = Path(local_path).resolve()
    try:
        rel = path.relative_to(root)
    except ValueError:
        raise DeviceError("Der gewählte Ordner liegt nicht auf der SD-Karte (bzw. ihrer Kopie).") from None
    parts = rel.parts
    if not parts:
        raise DeviceError("Bitte einen Ordner auf der SD-Karte wählen, nicht die ganze Karte.")
    return "/" + "/".join(parts)


def _local(sd_root: str, sd_path: str) -> Optional[Path]:
    parts = PurePosixPath(sd_path.replace("\\", "/")).parts
    parts = [p for p in parts if p != "/"]
    if not sd_root or not parts or ".." in parts:
        return None
    return Path(sd_root).joinpath(*parts)


def _audio_files(folder: Path, recursive: bool) -> List[Path]:
    pattern = folder.rglob("*") if recursive else folder.iterdir()
    files = [p for p in pattern if p.is_file() and p.suffix.lower() in AUDIO_EXTENSIONS
             and not p.name.startswith(("._", "."))]
    return sorted(files, key=lambda p: str(p.relative_to(folder)).casefold())


def card_files(sd_root: str, sd_path: str, play_mode: int) -> Tuple[List[str], bool]:
    """Dateien einer Karte relativ zu sd_root (mit '/'), und ob der Pfad gefunden wurde.
    Nicht gefunden: der SD-Pfad selbst (ohne fuehrenden '/'), damit er als fehlend erscheint."""
    rel_fallback = sd_path.strip("/")
    local = _local(sd_root, sd_path)
    if local is None:
        return [rel_fallback], False
    root = Path(sd_root)
    try:
        if local.is_file():
            return [local.relative_to(root).as_posix()], True
        if local.is_dir():
            files = _audio_files(local, play_mode in RECURSIVE_MODES)
            if files:
                return [f.relative_to(root).as_posix() for f in files], True
    except OSError as exc:  # SD-Karte abgezogen oder defekt (etwa WinError 1117)
        log.warning("SD-Karte: %s nicht lesbar: %s", local, exc)
    return [rel_fallback], False


@dataclass
class DeviceState:
    """Was gerade auf einem ESPuino ist: Zuweisungen (NVS) und Ordner der SD-Karte"""
    host: str
    reachable: bool = False
    error: str = ""
    firmware: str = ""
    ip: str = ""
    total_bytes: int = 0
    free_bytes: int = 0
    assignments: Dict[str, Tuple[str, int]] = field(default_factory=dict)  # Karte -> (Ordner, Modus)
    folders: Optional[List[str]] = None  # "/Ordner" im Wurzelverzeichnis; None = unbekannt
    folders_from_pc: bool = False  # Ordner von der SD-Karte im Kartenleser gelesen
    sd_error: str = ""  # SD-Karte im PC steckt, laesst sich aber nicht lesen
    backup_date: str = ""  # Zuweisungen aus backup.txt der SD-Karte (ESPuino nicht erreichbar): deren Stand
    skipped_function: int = 0
    skipped_webradio: int = 0
    skipped_mediahub: int = 0
    dirs: Dict[str, Set[str]] = field(default_factory=dict, repr=False)  # Zwischenspeicher je Ordner


def _parse_assignments(entries: List[dict], state: DeviceState) -> None:
    for entry in entries:
        card_id = str(entry.get("id", "")).strip()
        if not card_id.isdigit():
            continue
        if "modId" in entry and "fileOrUrl" not in entry:
            state.skipped_function += 1
            continue
        try:
            play_mode = int(entry.get("playMode") or 0)
        except (TypeError, ValueError):
            play_mode = 0
        target = str(entry.get("fileOrUrl") or "").strip()
        if play_mode == MEDIAHUB_MODE:
            state.skipped_mediahub += 1
        elif play_mode == WEBSTREAM or re.match(r"^[a-zA-Z]+://", target):
            state.skipped_webradio += 1
        elif target and target != "0" and play_mode > 0:
            state.assignments[card_id] = (target, play_mode)


def list_dirs(host: str, sd_path: str = "/") -> Set[str]:
    """Namen der Unterordner eines Ordners auf der SD-Karte des ESPuino (ueber das WLAN)"""
    entries = _request(host, "/explorer?path=" + urllib.parse.quote(sd_path), timeout=15)
    if not isinstance(entries, list):
        return set()
    return {str(e["name"]) for e in entries
            if isinstance(e, dict) and e.get("dir") and e.get("name") and e["name"] != "System Volume Information"}


NOT_CONNECTED = "Nicht verbunden"
BACKUP_FILE = "backup.txt"  # Sicherung der Zuweisungen, die der ESPuino auf seine SD-Karte schreibt
_BACKUP_LINE = re.compile(r"^\^(\d+)\^#(.*)#(\d+)#(\d+)#(\d+)\s*$")


def parse_backup(text: str) -> List[dict]:
    """backup.txt des ESPuino: je Zeile ^Karte^#Pfad#Position#Abspielmodus#Titel.
    Gibt Eintraege wie GET /rfid zurueck; Zeilen in anderem Format (etwa
    Funktionskarten) werden uebersprungen."""
    entries = []
    for line in text.lstrip(codecs.BOM_UTF8.decode("utf-8")).splitlines():
        match = _BACKUP_LINE.match(line.strip("\r"))
        if match:
            card_id, path, _pos, mode, _track = match.groups()
            entries.append({"id": card_id, "fileOrUrl": path, "playMode": int(mode)})
    return entries


def read_backup(sd_root: str, state: "DeviceState") -> bool:
    """Zuweisungen aus backup.txt der SD-Karte im Kartenleser in state uebernehmen.
    Nur ein Stand der Sicherung: der ESPuino schreibt sie bei jeder Aenderung neu."""
    path = Path(sd_root) / BACKUP_FILE
    try:
        text = path.read_text(encoding="utf-8-sig", errors="replace")
        stamp = path.stat().st_mtime
    except OSError:
        return False
    from datetime import datetime
    _parse_assignments(parse_backup(text), state)
    state.backup_date = datetime.fromtimestamp(stamp).strftime("%d.%m.%Y %H:%M")
    return True


def fetch_state(host: str, sd_root: str = "", connect: bool = True) -> DeviceState:
    """Zuweisungen, Platz und Ordner des ESPuino. Steckt seine SD-Karte im PC
    (sd_root vorhanden), kommen die Ordner von dort. Nicht erreichbar: reachable
    False und error gesetzt, die Ordner ggf. trotzdem aus dem Kartenleser.
    connect False: gar nicht erst im WLAN fragen (error NOT_CONNECTED)."""
    state = DeviceState(host=normalize_host(host))
    try:
        if not connect:
            raise DeviceError(NOT_CONNECTED)
        info = fetch_info(state.host)
        state.firmware = firmware_label(info)
        wifi = info.get("wifi") or {}
        state.ip = str(wifi.get("ip") or "") if isinstance(wifi, dict) else ""
        sd = info.get("sdcard") or {}
        state.total_bytes = int(sd.get("totalBytes") or 0)
        state.free_bytes = int(sd.get("freeBytes") or 0)
        _parse_assignments(fetch_assignments(state.host), state)
        state.reachable = True
    except (DeviceError, TypeError, ValueError) as exc:
        state.error = str(exc)
    local_folders = None
    if sd_root and os.path.isdir(sd_root):
        try:
            local_folders = sorted("/" + p.name for p in Path(sd_root).iterdir()
                                   if p.is_dir() and p.name != "System Volume Information"
                                   and not p.name.startswith("."))
        except OSError as exc:  # Karte steckt, laesst sich aber nicht lesen: wie ohne Karte weiter
            log.warning("SD-Karte %s nicht lesbar: %s", sd_root, exc)
            state.sd_error = f"SD-Karte im PC nicht lesbar ({exc.strerror or exc})"
    if local_folders is not None:
        if not state.reachable:
            read_backup(sd_root, state)
        state.folders_from_pc = True
        state.folders = local_folders
    elif state.reachable:
        try:
            state.dirs["/"] = list_dirs(state.host, "/")
            state.folders = sorted("/" + n for n in state.dirs["/"])
        except DeviceError:
            state.folders = None
    return state


def folder_exists(state: DeviceState, sd_root: str, sd_folder: str) -> Optional[bool]:
    """Gibt es den Ordner auf der SD-Karte? None, wenn sich das nicht sagen laesst"""
    parts = [p for p in sd_folder.replace("\\", "/").split("/") if p]
    if not parts:
        return None
    if state.folders_from_pc:
        try:
            return Path(sd_root).joinpath(*parts).is_dir()
        except OSError:
            return None
    if state.folders is None or not state.reachable:
        return None
    parent = "/" + "/".join(parts[:-1]) if len(parts) > 1 else "/"
    if parent not in state.dirs:
        try:
            state.dirs[parent] = list_dirs(state.host, parent)
        except DeviceError:
            return None
    return parts[-1].casefold() in {n.casefold() for n in state.dirs[parent]}


def same_folder(a: str, b: str) -> bool:
    return a.strip().strip("/").casefold() == b.strip().strip("/").casefold()


def build_cards(known, state: DeviceState, library_index, sd_root: str) -> Tuple[MediahubData, bool]:
    """Bekannte Karten (core/espuino_cards) mit dem Stand des Geraets und der
    Bibliothek zusammenfuehren. Zuweisungen des Geraets, die Studio noch nicht
    kennt, werden uebernommen; zeigt eine bekannte Karte auf dem Geraet auf einen
    anderen, vorhandenen Ordner, gilt das Geraet (dort wurde sie neu belegt).
    Gibt die Karten zurueck und ob sich das Verzeichnis geaendert hat."""
    from core.espuino_cards import ASSIGNED, ASSIGNED_OTHER, NOT_ASSIGNED, find_library_album
    changed = False
    for card_id, (target, play_mode) in state.assignments.items():
        card = known.get(card_id)
        if card is None:
            changed |= known.merge_assignment(card_id, target, play_mode)
        elif not same_folder(card.sd_folder, target) and folder_exists(state, sd_root, target):
            known.remember(card_id, name=target.strip("/").rpartition("/")[2], sd_folder=target,
                           play_mode=play_mode, library_path="", album_id="")
            changed = True

    device = Device(esp_id=state.host, alias=f"ESPuino {state.host}")
    result = MediahubData(devices={state.host: device}, skipped_function=state.skipped_function,
                          skipped_webradio=state.skipped_webradio, skipped_mediahub=state.skipped_mediahub)
    from core.library import in_system_folder
    for card in list(known.cards.values()):
        album = find_library_album(card, library_index)
        if album is None and card.library_path and in_system_folder(card.library_path):
            # frueher aus dem Papierkorb des NAS verknuepft: dieser Verweis gilt nicht mehr
            known.remember(card.card_id, library_path="", album_id="")
            changed = True
        if album is not None and (card.library_path != album.path
                                  or (album.album_id and card.album_id != album.album_id)):
            known.remember(card.card_id, library_path=album.path, album_id=album.album_id or card.album_id)
            changed = True
        if album is not None:
            local_tracks = list(album.tracks)
            root = Path(album.path)
            files = []
            for track in local_tracks:
                try:
                    files.append(Path(track).relative_to(root).as_posix())
                except ValueError:
                    files.append(os.path.basename(track))
        else:
            files, found = card_files(sd_root, card.sd_folder, card.play_mode)
            local_tracks = [str(Path(sd_root).joinpath(*f.split("/"))) for f in files] if found else []
        entry = state.assignments.get(card.card_id)
        if not state.reachable and not state.backup_date:
            status, target = "", ""
        elif entry is None:
            status, target = NOT_ASSIGNED, ""
        else:
            status = ASSIGNED if same_folder(entry[0], card.sd_folder) else ASSIGNED_OTHER
            target = entry[0]
        result.cards.append(MediahubCard(
            card_id=card.card_id, name=card.label, kind=KIND_FILES,
            play_mode=str(card.play_mode or ""), files=files,
            devices=[device] if entry is not None else [], source_esp=state.host,
            sd_path=card.sd_folder, local_tracks=local_tracks,
            library_path=album.path if album is not None else "",
            library_album_id=album.album_id if album is not None else "",
            device_status=status, device_target=target,
            folder_present=folder_exists(state, sd_root, card.sd_folder) if card.sd_folder else None,
        ))
    result.cards.sort(key=lambda c: (c.label.casefold(), c.card_id))
    return result, changed


def load_cards(host: str, sd_root: str, known=None, library_index=None) -> MediahubData:
    """Karten eines ESPuino (ohne gespeichertes Verzeichnis: nur die des Geraets)"""
    from core.espuino_cards import KnownCards
    state = fetch_state(host, sd_root)
    if not state.reachable:
        raise DeviceError(state.error)
    data, _changed = build_cards(known if known is not None else KnownCards(), state, library_index, sd_root)
    return data


# --- Mehrere ESPuinos -------------------------------------------------------------
#
# Gespeichert als JSON-Liste unter SETTINGS_DEVICES; SETTINGS_HOST bleibt die Adresse
# des zuletzt verbundenen Geraets (aeltere Fassungen kannten nur dieses eine).

@dataclass
class SavedDevice:
    """Ein eingerichteter ESPuino: Name (frei oder sein Hostname), Adresse im WLAN
    und der Ordner seiner SD-Karte auf dem PC (leer: steckt nicht im Kartenleser)"""
    host: str
    name: str = ""
    sd_root: str = ""

    @property
    def label(self) -> str:
        return self.name or self.host


def _same_host(a: str, b: str) -> bool:
    return a.strip().casefold() == b.strip().casefold()


def saved_devices() -> List[SavedDevice]:
    """Alle eingerichteten ESPuinos; uebernimmt die Einzeladresse aelterer Fassungen"""
    from PyQt6.QtCore import QSettings
    s = QSettings()
    try:
        raw = json.loads(s.value(SETTINGS_DEVICES, "", type=str) or "[]")
    except json.JSONDecodeError:
        raw = []
    devices: List[SavedDevice] = []
    for entry in raw if isinstance(raw, list) else []:
        if isinstance(entry, dict) and str(entry.get("host") or "").strip():
            host = str(entry["host"]).strip()
            if not any(_same_host(d.host, host) for d in devices):
                devices.append(SavedDevice(host, str(entry.get("name") or ""), str(entry.get("sd_root") or "")))
    legacy = s.value(SETTINGS_HOST, "", type=str).strip()
    if legacy and not any(_same_host(d.host, legacy) for d in devices):
        devices.insert(0, SavedDevice(legacy, "", s.value(SETTINGS_SD_ROOT, "", type=str)))
    return devices


def store_devices(devices: List[SavedDevice]) -> None:
    from PyQt6.QtCore import QSettings
    QSettings().setValue(SETTINGS_DEVICES, json.dumps(
        [{"host": d.host, "name": d.name, "sd_root": d.sd_root} for d in devices], ensure_ascii=False))


def find_device(host: str) -> Optional[SavedDevice]:
    return next((d for d in saved_devices() if _same_host(d.host, host)), None)


def remember_device(host: str, name: Optional[str] = None, sd_root: Optional[str] = None) -> SavedDevice:
    """Geraet anlegen oder aendern (None: Wert bleibt)"""
    devices = saved_devices()
    device = next((d for d in devices if _same_host(d.host, host)), None)
    if device is None:
        device = SavedDevice(host)
        devices.append(device)
    if name is not None:
        device.name = name
    if sd_root is not None:
        device.sd_root = sd_root
    store_devices(devices)
    return device


def forget_device(host: str) -> None:
    from PyQt6.QtCore import QSettings
    store_devices([d for d in saved_devices() if not _same_host(d.host, host)])
    s = QSettings()
    if _same_host(s.value(SETTINGS_HOST, "", type=str), host):
        s.setValue(SETTINGS_HOST, "")
        s.setValue(SETTINGS_SD_ROOT, "")


def current_host() -> str:
    from PyQt6.QtCore import QSettings
    return QSettings().value(SETTINGS_HOST, "", type=str)


def set_current(host: str) -> None:
    """Mit diesem Geraet arbeiten (leer: mit keinem)"""
    from PyQt6.QtCore import QSettings
    s = QSettings()
    device = find_device(host) if host else None
    s.setValue(SETTINGS_HOST, device.host if device else "")
    s.setValue(SETTINGS_SD_ROOT, device.sd_root if device else "")


def fetch_hostname(host: str, timeout: float = TIMEOUT) -> str:
    """Hostname, den der ESPuino in seinen WLAN-Einstellungen fuehrt ("" wenn unbekannt)"""
    try:
        data = _request(host, "/settings?section=wifi", timeout=timeout)
    except DeviceError:
        return ""
    wifi = data.get("wifi", data) if isinstance(data, dict) else {}
    return str(wifi.get("hostname") or "").strip() if isinstance(wifi, dict) else ""


@dataclass
class Probe:
    """Ergebnis einer kurzen Pruefung, ob ein ESPuino gerade antwortet"""
    host: str
    reachable: bool = False
    ip: str = ""
    hostname: str = ""
    firmware: str = ""
    error: str = ""


PROBE_TIMEOUT = 3


def probe(host: str, timeout: float = PROBE_TIMEOUT) -> Probe:
    result = Probe(host)
    try:
        info = fetch_info(host, timeout)
    except DeviceError as exc:
        result.error = str(exc)
        return result
    result.reachable = True
    result.firmware = firmware_label(info)
    wifi = info.get("wifi") or {}
    result.ip = str(wifi.get("ip") or "") if isinstance(wifi, dict) else ""
    result.hostname = fetch_hostname(host, timeout)
    return result


def settings() -> Tuple[str, str, str]:
    """(Quelle, Adresse des ESPuino, SD-Karte auf dem PC). Wer schon einen
    Mediahub eingerichtet hat, behaelt ihn als Quelle."""
    from PyQt6.QtCore import QSettings
    from core.mediahub_cards import SETTINGS_DB_PATH
    s = QSettings()
    source = s.value(SETTINGS_SOURCE, "", type=str)
    if source not in SOURCES:
        source = SOURCE_MEDIAHUB if s.value(SETTINGS_DB_PATH, "", type=str) else SOURCE_DEVICE
    return source, s.value(SETTINGS_HOST, "", type=str), s.value(SETTINGS_SD_ROOT, "", type=str)


def save_settings(source: str, host: str, sd_root: str, name: Optional[str] = None) -> None:
    """Quelle speichern; mit Adresse wird der ESPuino (in der Liste) zum aktuellen"""
    if source not in SOURCES:
        raise ValueError(f"Unbekannte Quelle: {source!r}")
    from PyQt6.QtCore import QSettings
    s = QSettings()
    s.setValue(SETTINGS_SOURCE, source)
    if host:
        remember_device(host, name=name, sd_root=sd_root)
    s.setValue(SETTINGS_HOST, host)
    s.setValue(SETTINGS_SD_ROOT, sd_root)


def use_acr122u() -> bool:
    """ACR122U im ESPuino-Modus nutzen? Standard aus: der Leser wird dann gar nicht angesprochen"""
    from PyQt6.QtCore import QSettings
    return QSettings().value(SETTINGS_USE_ACR122U, False, type=bool)


def set_use_acr122u(value: bool) -> None:
    from PyQt6.QtCore import QSettings
    QSettings().setValue(SETTINGS_USE_ACR122U, bool(value))


def ask_on_start() -> bool:
    from PyQt6.QtCore import QSettings
    return QSettings().value(SETTINGS_ASK_ON_START, True, type=bool)


def set_ask_on_start(value: bool) -> None:
    from PyQt6.QtCore import QSettings
    QSettings().setValue(SETTINGS_ASK_ON_START, bool(value))


def is_audio_file(path: str) -> bool:
    return os.path.splitext(path)[1].lower() in AUDIO_EXTENSIONS
