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

import json
import os
import re
import urllib.error
import urllib.request
from pathlib import Path, PurePosixPath
from typing import List, Optional, Tuple

from core.mediahub_cards import KIND_FILES, Device, MediahubCard, MediahubData

SETTINGS_SOURCE = "espuino/source"
SETTINGS_HOST = "espuino/host"
SETTINGS_SD_ROOT = "espuino/sd_root"
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


def fetch_info(host: str) -> dict:
    info = _request(host, "/info")
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
    if local.is_file():
        return [local.relative_to(root).as_posix()], True
    if local.is_dir():
        files = _audio_files(local, play_mode in RECURSIVE_MODES)
        if files:
            return [f.relative_to(root).as_posix() for f in files], True
    return [rel_fallback], False


def cards_from_assignments(entries: List[dict], host: str, sd_root: str) -> MediahubData:
    """Zuweisungen des ESPuino als MediahubData, damit Kartenliste, Cover-Manager,
    KI-Stapel und Druckbogen unveraendert funktionieren"""
    device = Device(esp_id=host, alias=f"ESPuino {host}")
    result = MediahubData(devices={host: device})
    for entry in entries:
        card_id = str(entry.get("id", "")).strip()
        if not card_id.isdigit():
            continue
        if "modId" in entry and "fileOrUrl" not in entry:
            result.skipped_function += 1
            continue
        try:
            play_mode = int(entry.get("playMode") or 0)
        except (TypeError, ValueError):
            play_mode = 0
        target = str(entry.get("fileOrUrl") or "").strip()
        if play_mode == MEDIAHUB_MODE:
            result.skipped_mediahub += 1
            continue
        if play_mode == WEBSTREAM or re.match(r"^[a-zA-Z]+://", target):
            result.skipped_webradio += 1
            continue
        if not target or target == "0":
            result.skipped_empty += 1
            continue
        files, _found = card_files(sd_root, target, play_mode)
        result.cards.append(MediahubCard(
            card_id=card_id,
            name=PurePosixPath(target.rstrip("/")).name or target,
            kind=KIND_FILES,
            play_mode=str(play_mode) if play_mode else "",
            files=files,
            devices=[device],
            source_esp=host,
            sd_path=target,
        ))
    result.cards.sort(key=lambda c: (c.label.casefold(), c.card_id))
    return result


def load_cards(host: str, sd_root: str) -> MediahubData:
    host = normalize_host(host)
    return cards_from_assignments(fetch_assignments(host), host, sd_root)


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


def save_settings(source: str, host: str, sd_root: str) -> None:
    if source not in SOURCES:
        raise ValueError(f"Unbekannte Quelle: {source!r}")
    from PyQt6.QtCore import QSettings
    s = QSettings()
    s.setValue(SETTINGS_SOURCE, source)
    s.setValue(SETTINGS_HOST, host)
    s.setValue(SETTINGS_SD_ROOT, sd_root)


def is_audio_file(path: str) -> bool:
    return os.path.splitext(path)[1].lower() in AUDIO_EXTENSIONS
