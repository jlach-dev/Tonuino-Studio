"""
Karten aus dem ESPuino-Mediahub (biologist79/ESPuino-Mediahub) lesen.

Der Mediahub speichert alles in einer Datei db.json in seinem Datenordner:
    {"settings": {...},
     "devices": {"<esp_id>": {"alias": ..., "ip": ..., "last_seen": ...}},
     "cards": {"<esp_id>/<card_id>": {"esp_id", "card_id" (12 Ziffern),
               "status": "pending"|"assigned", "name", "kind": "files"|"webradio",
               "play_mode", "stream_url", "files": [{"path", "size", "sha256"}],
               "updated_at", ...}}}
"path" ist relativ zur Mediathek (im Container /media) und mit "/" getrennt.

Die Datei wird hier nur gelesen, nie geschrieben, und kein Endpunkt des
Mediahub wird aufgerufen (der Manifest-Endpunkt registriert dabei Geraete und
Karten). Der Zugriff ist in diesem Modul gekapselt, damit er spaeter auf eine
Lese-API umgestellt werden kann.

Eine physische Karte kann auf mehreren ESPuinos zugewiesen sein. Sie bekommt
ein Kartencover; bei unterschiedlichem Inhalt gilt die neueste Zuweisung.

Eigene Datei des Forks (siehe core/branding.py).
"""

import json
import os
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Dict, List, Optional, Tuple

from core.cover_archive import MEDIAHUB_ALBUM_PREFIX
from core.sd_card import Folder, Track

DB_FILENAME = "db.json"
ALBUM_ID_PREFIX = MEDIAHUB_ALBUM_PREFIX
SETTINGS_DB_PATH = "mediahub/db_path"
SETTINGS_MEDIA_ROOT = "mediahub/media_root"

STATUS_ASSIGNED = "assigned"
KIND_FILES = "files"
KIND_WEBRADIO = "webradio"

# Abspielmodi des ESPuino (Zahlen wie in manifest.py des Mediahub)
PLAY_MODE_LABELS = {
    1: "Einzelner Titel", 2: "Einzelner Titel (Endlosschleife)",
    3: "Hörbuch (merkt sich die Stelle)", 4: "Hörbuch (Endlosschleife)",
    16: "Hörbuch, mit Unterordnern (merkt sich die Stelle)",
    5: "Alle Titel des Ordners (sortiert)", 6: "Alle Titel des Ordners (zufällig)",
    7: "Alle Titel des Ordners (sortiert, Endlosschleife)", 9: "Alle Titel des Ordners (zufällig, Endlosschleife)",
    17: "Alle Titel mit Unterordnern (zufällig)", 15: "Alle Titel mit Unterordnern (sortiert)",
    12: "Ein zufälliger Titel des Ordners",
    13: "Zufälliger Unterordner (sortiert)", 14: "Zufälliger Unterordner (zufällig)",
}


def play_mode_label(play_mode: str) -> str:
    try:
        return PLAY_MODE_LABELS.get(int(play_mode), f"Modus {play_mode}")
    except (TypeError, ValueError):
        return str(play_mode)


class MediahubError(Exception):
    """db.json fehlt, ist unlesbar oder hat nicht das Mediahub-Format"""


@dataclass
class Device:
    esp_id: str
    alias: str = ""

    @property
    def label(self) -> str:
        return self.alias or self.esp_id


@dataclass
class MediahubCard:
    card_id: str
    name: str = ""
    kind: str = KIND_FILES
    play_mode: str = ""
    files: List[str] = field(default_factory=list)  # relativ zur Mediathek, mit "/"
    updated_at: str = ""
    devices: List[Device] = field(default_factory=list)  # Geraete, auf denen die Karte zugewiesen ist
    source_esp: str = ""  # Geraet der Zuweisung, die gilt (die neueste)
    conflict: bool = False  # auf den Geraeten unterschiedlicher Inhalt
    sd_path: str = ""  # nur bei Karten direkt vom ESPuino: Ziel auf seiner SD-Karte ("/Ordner")

    @property
    def album_id(self) -> str:
        return album_id_for(self.card_id)

    @property
    def label(self) -> str:
        return self.name.strip() or f"Karte {self.card_id}"


@dataclass
class MediahubData:
    cards: List[MediahubCard] = field(default_factory=list)
    devices: Dict[str, Device] = field(default_factory=dict)
    skipped_webradio: int = 0
    skipped_empty: int = 0
    pending: int = 0
    skipped_function: int = 0  # nur direkt vom ESPuino: Funktionskarten (Lautstaerke, Pause, ...)
    skipped_mediahub: int = 0  # nur direkt vom ESPuino: Karten, die der Mediahub verwaltet


@dataclass
class MediahubFolder(Folder):
    """Folder fuer den Cover-Manager, Druckbogen und KI-Stapel: ohne Nummer
    (index=None), Kartendateien im Cover-Archiv statt im Musikordner"""
    card_id: str = ""
    album_id: str = ""
    card_dir: str = ""
    missing_files: List[str] = field(default_factory=list)  # wie in db.json, in der Mediathek nicht gefunden

    @property
    def missing(self) -> int:
        return len(self.missing_files)


def album_id_for(card_id: str) -> str:
    """Feste Album-ID je Karte. Nicht aus den Tags der Dateien: eine Karte kann
    eine gemischte Wiedergabeliste sein, deren Dateien zu anderen Alben gehoeren."""
    card_id = str(card_id)
    if not card_id or not card_id.isdigit():
        raise MediahubError(f"Ungültige Kartennummer: {card_id!r}")
    return ALBUM_ID_PREFIX + card_id


def db_file(path: str) -> Path:
    """Pfad zur db.json; erlaubt ist die Datei selbst oder der Datenordner"""
    p = Path(path)
    return p / DB_FILENAME if p.is_dir() else p


def _read_db(path: str) -> dict:
    file = db_file(path)
    try:
        with open(file, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except FileNotFoundError:
        raise MediahubError(f"Keine {DB_FILENAME} gefunden: {file}") from None
    except (OSError, UnicodeDecodeError) as exc:
        raise MediahubError(f"{file} lässt sich nicht lesen: {exc}") from None
    except json.JSONDecodeError as exc:
        raise MediahubError(f"{file} ist kein gültiges JSON (Zeile {exc.lineno}). "
                            "Schreibt der Mediahub gerade? Bitte noch einmal einlesen.") from None
    if not isinstance(data, dict) or not isinstance(data.get("cards", {}), dict):
        raise MediahubError(f"{file} sieht nicht wie die Datenbank des ESPuino-Mediahub aus.")
    return data


def _file_paths(entry: dict) -> List[str]:
    paths = []
    for item in entry.get("files") or []:
        path = item.get("path") if isinstance(item, dict) else item
        if isinstance(path, str) and path.strip():
            paths.append(path.strip())
    return paths


def _content(entry: dict) -> tuple:
    return (entry.get("kind") or KIND_FILES, tuple(_file_paths(entry)), entry.get("stream_url") or "")


def load_cards(path: str) -> MediahubData:
    """Zugewiesene Karten aus db.json, eine je Kartennummer, sortiert nach Name"""
    data = _read_db(path)
    devices: Dict[str, Device] = {}
    for esp_id, info in (data.get("devices") or {}).items():
        alias = info.get("alias", "") if isinstance(info, dict) else ""
        devices[str(esp_id)] = Device(str(esp_id), str(alias or ""))

    result = MediahubData(devices=devices)
    groups: Dict[str, List[dict]] = {}
    for key, entry in data["cards"].items():
        if not isinstance(entry, dict):
            continue
        card_id = str(entry.get("card_id") or str(key).rpartition("/")[2])
        esp_id = str(entry.get("esp_id") or str(key).partition("/")[0])
        if not card_id.isdigit():
            continue
        if entry.get("status") != STATUS_ASSIGNED:
            result.pending += 1
            continue
        groups.setdefault(card_id, []).append(dict(entry, esp_id=esp_id, card_id=card_id))

    for card_id, entries in groups.items():
        entries.sort(key=lambda e: str(e.get("updated_at") or ""), reverse=True)
        newest = entries[0]
        kind = newest.get("kind") or KIND_FILES
        if kind == KIND_WEBRADIO:
            result.skipped_webradio += 1
            continue
        files = _file_paths(newest)
        if kind != KIND_FILES or not files:
            result.skipped_empty += 1
            continue
        card_devices = []
        for entry in entries:
            esp_id = entry["esp_id"]
            if all(d.esp_id != esp_id for d in card_devices):
                card_devices.append(devices.get(esp_id) or Device(esp_id))
        result.cards.append(MediahubCard(
            card_id=card_id,
            name=str(newest.get("name") or ""),
            kind=kind,
            play_mode=str(newest.get("play_mode") if newest.get("play_mode") is not None else ""),
            files=files,
            updated_at=str(newest.get("updated_at") or ""),
            devices=card_devices,
            source_esp=newest["esp_id"],
            conflict=len({_content(e) for e in entries}) > 1,
        ))
    result.cards.sort(key=lambda c: (c.label.casefold(), c.card_id))
    return result


def _resolve(card: MediahubCard, media_root: str) -> Tuple[List[str], List[str]]:
    if not media_root:
        return [], list(card.files)
    root = Path(media_root).resolve()
    found, missing = [], []
    for rel in card.files:
        parts = PurePosixPath(rel.replace("\\", "/")).parts
        if not parts or parts[0] == "/" or ".." in parts or ":" in parts[0]:
            missing.append(rel)
            continue
        path = root.joinpath(*parts)
        if path.is_file():
            found.append(str(path))
        else:
            missing.append(rel)
    return found, missing


def resolve_files(card: MediahubCard, media_root: str) -> Tuple[List[str], int]:
    """Pfade der Karte auf dem PC (vorhandene Dateien) und Zahl der fehlenden.
    Pfade, die aus der Mediathek hinausfuehren (.., absolut), zaehlen als fehlend."""
    found, missing = _resolve(card, media_root)
    return found, len(missing)


def build_folder(card: MediahubCard, media_root: str, archive) -> MediahubFolder:
    """Folder fuer den Cover-Manager. path ist der gemeinsame Ordner der
    vorhandenen Dateien (fuer Ordner-Bilder bei der Cover-Suche), die
    Kartendateien liegen in archive.card_dir(album_id)."""
    from core.card_store import card_paths
    paths, missing = _resolve(card, media_root)
    if paths:
        common = os.path.commonpath(paths)
        folder_path = common if os.path.isdir(common) else os.path.dirname(common)
    else:
        folder_path = media_root or ""
    album_id = card.album_id
    card_dir = str(archive.card_dir(album_id)) if archive is not None else ""
    card_png = card_paths(card_dir)[0] if card_dir else None
    tracks = [Track(index=i, filename=os.path.basename(p), filepath=p) for i, p in enumerate(paths, 1)]
    return MediahubFolder(
        index=None, path=folder_path, tracks=tracks, name=card.label,
        card_path=str(card_png) if card_png is not None and card_png.is_file() else "",
        card_id=card.card_id, album_id=album_id, card_dir=card_dir, missing_files=missing,
    )


def format_card_id(card_id: str) -> str:
    """Kartennummer lesbar: 12 Ziffern als vier Dreiergruppen (die UID-Bytes)"""
    if len(card_id) == 12 and card_id.isdigit():
        return " ".join(card_id[i:i + 3] for i in range(0, 12, 3))
    return card_id


def settings_paths() -> Tuple[str, str]:
    """(db.json bzw. Datenordner, Mediathek) aus den Einstellungen"""
    from PyQt6.QtCore import QSettings
    s = QSettings()
    return s.value(SETTINGS_DB_PATH, "", type=str), s.value(SETTINGS_MEDIA_ROOT, "", type=str)


def save_settings_paths(db_path: str, media_root: str) -> None:
    from PyQt6.QtCore import QSettings
    s = QSettings()
    s.setValue(SETTINGS_DB_PATH, db_path)
    s.setValue(SETTINGS_MEDIA_ROOT, media_root)


def card_by_id(data: Optional[MediahubData], card_id: str) -> Optional[MediahubCard]:
    if data is None:
        return None
    return next((c for c in data.cards if c.card_id == card_id), None)
