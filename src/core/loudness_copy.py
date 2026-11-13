"""
Lautstaerke beim Uebertragen auf den ESPuino angleichen (ESPuino-Modus).

Anders als im TonUINO-Modus bleibt die Datei in der Bibliothek unveraendert:
gemessen wird das Original, angeglichen wird eine Kopie in einem temporaeren
Ordner, und diese Kopie geht statt des Originals auf die SD-Karte. Messung,
Zielwert und Begrenzer sind dieselben wie im TonUINO-Modus (core/loudness.py).

Nur MP3: die Kopie ist immer eine MP3-Datei, und der Zielname auf der SD-Karte
behaelt seine Endung. Andere Formate gehen unveraendert hinueber.

Eigene Datei des Forks (siehe core/branding.py).
"""

import logging
import os
import shutil
import tempfile
from typing import List, Optional

from core import loudness

log = logging.getLogger(__name__)

SETTINGS_NORMALIZE_ON_TRANSFER = "espuino/normalize_on_transfer"


def normalize_on_transfer() -> bool:
    """Gemerkte Wahl aus dem Uebertragen-Dialog (Standard: aus, weil verlustbehaftet)"""
    from PyQt6.QtCore import QSettings
    return QSettings().value(SETTINGS_NORMALIZE_ON_TRANSFER, False, type=bool)


def set_normalize_on_transfer(enabled: bool) -> None:
    from PyQt6.QtCore import QSettings
    QSettings().setValue(SETTINGS_NORMALIZE_ON_TRANSFER, bool(enabled))


def source_bitrate(filepath: str) -> Optional[int]:
    """Bitrate der Quelle in bit/s, damit die Kopie nicht schlechter wird"""
    try:
        from mutagen.mp3 import MP3
        return int(MP3(filepath).info.bitrate)
    except Exception:
        return None


def is_mp3(path: str) -> bool:
    return os.path.splitext(path)[1].lower() == ".mp3"


class TransferNormalizer:
    """Wird je Datei vor dem Kopieren bzw. Hochladen aufgerufen und liefert den
    Pfad der Datei, die wirklich uebertragen wird: das Original (passt schon,
    kein MP3, Fehler) oder eine angeglichene Kopie. Die Kopie liegt immer unter
    demselben temporaeren Namen und wird von der naechsten ueberschrieben -
    uebertragen wird ja eine Datei nach der anderen."""

    def __init__(self, converter):
        self.converter = converter
        self.adjusted = 0
        self.unchanged = 0
        self.not_mp3 = 0
        self.errors: List[str] = []
        self._dir: Optional[str] = None

    def __call__(self, source: str) -> str:
        if not is_mp3(source):
            self.not_mp3 += 1
            return source
        try:
            gain = loudness.gain_for(self.converter.measure_loudness(source))
            if gain is None:
                self.unchanged += 1
                return source
            if self._dir is None:
                self._dir = tempfile.mkdtemp(prefix="tonuino-studio-lautstaerke-")
            target = os.path.join(self._dir, "angeglichen.mp3")
            bitrate = loudness.pick_bitrate_kbps(source_bitrate(source))
            self.converter.apply_gain(source, target, gain, bitrate)
            self.adjusted += 1
            return target
        except Exception as exc:  # dann eben das Original - die Uebertragung soll nicht scheitern
            log.warning("Lautstaerke beim Uebertragen nicht angeglichen (%s): %s", os.path.basename(source), exc)
            self.errors.append(f"{os.path.basename(source)}: {exc}")
            return source

    def cleanup(self) -> None:
        if self._dir is not None:
            shutil.rmtree(self._dir, ignore_errors=True)
            self._dir = None

    def summary(self) -> str:
        """Kurzer Text fuer die Statuszeile; leer, wenn nichts gemessen wurde"""
        parts = []
        if self.adjusted:
            parts.append(f"Lautstärke von {self.adjusted} Datei{'' if self.adjusted == 1 else 'en'} angeglichen")
        if self.unchanged:
            parts.append(f"{self.unchanged} passte{'' if self.unchanged == 1 else 'n'} schon")
        if self.not_mp3:
            parts.append(f"{self.not_mp3} kein MP3, unverändert")
        if self.errors:
            parts.append(f"{len(self.errors)} nicht messbar, unverändert")
        return ", ".join(parts)
