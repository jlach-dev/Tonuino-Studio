"""
Hintergrund-Threads des Tonuino-Managers (Scan, Dateiarbeit, RFID, Serial)
"""

import logging
import os
import shutil
from pathlib import Path
from typing import Optional

from PyQt6.QtCore import QObject, QThread, pyqtSignal

from core.sd_card import SDCard, Folder, Track, MAX_TRACKS_PER_FOLDER
from core.audio_converter import AudioConverter
from core import loudness
from core.fileutil import replace_with_retry
from core.metadata import MetadataManager
from core.rfid import RFIDReader, RfidStatus
from core.tonuino_serial import TonuinoSerial, TonuinoSerialError, TonuinoWriteCancelled

log = logging.getLogger(__name__)


class _WorkerReaper(QObject):
    """Haelt ausgemusterte, noch laufende Worker am Leben, bis sie fertig
    sind. Ein QThread, dessen letzte Python-Referenz verschwindet, waehrend
    er noch laeuft, reisst die App mit "QThread: Destroyed while thread is
    still running" herunter. Lebt im GUI-Thread; finished kommt deshalb
    per Queued Connection hier an."""

    def __init__(self):
        super().__init__()
        self._workers = set()

    def adopt(self, worker: QThread):
        self._workers.add(worker)
        worker.finished.connect(self._release)
        if worker.isFinished():
            # finished kam womoeglich schon vor dem connect
            self._discard(worker)

    def _release(self):
        self._discard(self.sender())

    def _discard(self, worker):
        if worker in self._workers:
            worker.wait()  # kehrt nach finished sofort zurueck
            self._workers.discard(worker)

    def wait_all(self, msecs: int):
        for worker in list(self._workers):
            worker.wait(msecs)

    @property
    def count(self) -> int:
        return len(self._workers)


_reaper: Optional[_WorkerReaper] = None


def _worker_signals(worker: QThread) -> set:
    """Namen der eigenen Signale eines Workers (ohne die von QThread/QObject)"""
    names = {"finished"}
    for cls in type(worker).__mro__:
        if cls is QThread:
            break
        names.update(name for name, value in vars(cls).items() if isinstance(value, pyqtSignal))
    return names


class BackgroundTask(QThread):
    """Fuehrt fn(task) im Hintergrund aus"""
    done = pyqtSignal(object)
    failed = pyqtSignal(str)
    progress = pyqtSignal(int, int, str)

    def __init__(self, fn):
        super().__init__()
        self.fn = fn
        self._cancelled = False

    def cancel(self):
        self._cancelled = True

    @property
    def cancelled(self) -> bool:
        return self._cancelled

    def run(self):
        try:
            self.done.emit(self.fn(self))
        except Exception as e:
            log.exception("Hintergrundaufgabe fehlgeschlagen")
            self.failed.emit(str(e) or type(e).__name__)


def retire_worker(worker: Optional[QThread]):
    """Worker ausmustern, der durch einen neuen ersetzt wird (oder dessen
    Fenster schliesst), ohne den GUI-Thread mit wait() zu blockieren:
    abbrechen, alle Signale trennen (spaete Ergebnisse sollen nichts mehr
    ueberschreiben) und eine Referenz halten, bis der Thread wirklich endet."""
    global _reaper
    if worker is None:
        return
    if hasattr(worker, "cancel"):
        worker.cancel()
    for name in _worker_signals(worker):
        try:
            getattr(worker, name).disconnect()
        except (TypeError, RuntimeError):
            pass  # keine Verbindungen
    if worker.isRunning():
        if _reaper is None:
            _reaper = _WorkerReaper()
        _reaper.adopt(worker)


def wait_for_retired_workers(msecs: int = 3000):
    """Beim Beenden der App: ausgemusterte Worker noch auslaufen lassen"""
    if _reaper is not None:
        _reaper.wait_all(msecs)


def retired_worker_count() -> int:
    return _reaper.count if _reaper is not None else 0


class SDCardScanner(QThread):
    """Thread fuer das Scannen der SD-Karte"""
    completed = pyqtSignal(bool)
    progress = pyqtSignal(int)
    
    def __init__(self, sd_card: SDCard):
        super().__init__()
        self.sd_card = sd_card
    
    def run(self):
        try:
            self.progress.emit(10)
            # Kurze Pause um Signal zu verarbeiten
            self.msleep(50)
            success = self.sd_card.scan()
            self.progress.emit(100)
            self.completed.emit(success)
        except Exception:
            log.exception("Scan-Fehler")
            self.completed.emit(False)


class TrackAddWorker(QThread):
    """Thread zum Hinzufuegen (Kopieren/Konvertieren) von Tracks, damit die
    UI waehrend FFmpeg-Konvertierung/Datei-I/O nicht einfriert."""
    progress = pyqtSignal(int, int, str)  # aktueller Index, Gesamtzahl, Dateiname
    track_added = pyqtSignal(object)  # neuer Track
    error = pyqtSignal(str, str)  # Dateiname, Fehlermeldung
    completed = pyqtSignal(int)  # Anzahl erfolgreich hinzugefuegter Tracks

    def __init__(self, folder: Folder, filepaths: list,
                 audio_converter: AudioConverter, metadata_manager: MetadataManager):
        super().__init__()
        self.folder = folder
        self.filepaths = filepaths
        self.audio_converter = audio_converter
        self.metadata_manager = metadata_manager
        self._cancelled = False
        # Bereits belegte Tracknummern werden hier fortlaufend gefuehrt, da die
        # eigentliche Ordnerliste (folder.tracks) erst im GUI-Thread aktualisiert wird
        self._used_numbers = {t.index for t in folder.tracks}

    def cancel(self):
        self._cancelled = True

    @property
    def is_cancelled(self) -> bool:
        return self._cancelled

    def run(self):
        added = 0
        # Hat der Ordner einen manuell vergebenen Namen (steht im ersten Track),
        # erben ihn die neuen Tracks; sonst bleibt der Ordner unberuehrt
        first = min(self.folder.tracks, key=lambda t: t.index, default=None)
        folder_name = self.metadata_manager.read_metadata(first.filepath).folder_name if first else ""
        for i, filepath in enumerate(self.filepaths, start=1):
            if self._cancelled:
                break

            self.progress.emit(i, len(self.filepaths), os.path.basename(filepath))

            # Auch auf der Karte nachsehen: der Ordner kann seit dem letzten Scan
            # Dateien bekommen haben (z.B. im Explorer kopiert) - nie ueberschreiben
            next_number = 1
            on_disk = SDCard.track_numbers_on_disk(Path(self.folder.path))
            while next_number in self._used_numbers or next_number in on_disk:
                next_number += 1
            if next_number > MAX_TRACKS_PER_FOLDER:
                break

            dest_filename = f"{next_number:03d}.mp3"
            dest_path = Path(self.folder.path) / dest_filename
            # Erst unter einem Zwischennamen schreiben und taggen, dann umbenennen:
            # bricht das Kopieren ab (NAS weg, FFmpeg-Fehler), bleibt kein halber
            # Track als NNN.mp3 liegen, den TonUINO abspielen wuerde
            part_path = Path(self.folder.path) / f".tmp_add_{dest_filename}"

            try:
                if self.audio_converter.needs_conversion(filepath):
                    if not self.audio_converter.is_available:
                        self.error.emit(os.path.basename(filepath), "FFmpeg ist nicht verfügbar.")
                        continue
                    self.audio_converter.convert_to_mp3(filepath, str(part_path))
                else:
                    shutil.copy2(filepath, part_path)

                # Tags stehen jetzt in der Zieldatei (bei Konvertierung von FFmpeg
                # uebernommen, bei MP3 mitkopiert) - dort lesen statt in der Quelle,
                # die ggf. kein MP3 ist und von mutagen.mp3 nicht gelesen werden kann.
                metadata = self.metadata_manager.read_metadata(str(part_path))
                if metadata.title or folder_name:
                    self.metadata_manager.write_metadata(
                        str(part_path),
                        track_number=next_number if metadata.title else None,
                        folder_name=folder_name or None,
                    )
                replace_with_retry(part_path, dest_path)

                new_track = Track(
                    index=next_number,
                    filename=dest_filename,
                    filepath=str(dest_path),
                    title=metadata.title
                )
                self._used_numbers.add(next_number)
                self.track_added.emit(new_track)
                added += 1

            except Exception as e:
                try:
                    part_path.unlink(missing_ok=True)
                except OSError:
                    pass
                self.error.emit(os.path.basename(filepath), str(e))

        self.completed.emit(added)


class FolderRenameWorker(QThread):
    """Schreibt den manuell vergebenen Ordnernamen (leer = entfernen) in die Tags
    aller Tracks des Ordners; bei vielen Dateien auf der SD-Karte dauert das."""
    progress = pyqtSignal(int, int)  # aktueller Index, Gesamtzahl
    completed = pyqtSignal(int, str)  # Ordnernummer, Fehlertext (leer bei Erfolg)

    def __init__(self, folder: Folder, name: str, metadata_manager: MetadataManager):
        super().__init__()
        self.folder_index = folder.index
        self.filepaths = [t.filepath for t in folder.tracks]
        self.name = name
        self.metadata_manager = metadata_manager

    def run(self):
        failed = 0
        for i, filepath in enumerate(self.filepaths, start=1):
            self.progress.emit(i, len(self.filepaths))
            if not self.metadata_manager.write_metadata(filepath, folder_name=self.name):
                failed += 1
        error = f"{failed} von {len(self.filepaths)} Dateien konnten nicht beschrieben werden." if failed else ""
        self.completed.emit(self.folder_index, error)


class LoudnessAnalysisWorker(QThread):
    """Misst die Lautheit aller Tracks eines Ordners (nur lesend), damit die UI
    waehrend der FFmpeg-Aufrufe nicht einfriert."""
    progress = pyqtSignal(int, int, str)  # aktueller Index, Gesamtzahl, Dateiname
    # Track, gemessene Lautheit in LUFS (None bei Stille/Fehler), Fehlertext (leer bei Erfolg)
    measured = pyqtSignal(object, object, str)
    completed = pyqtSignal()

    def __init__(self, tracks: list, audio_converter: AudioConverter):
        super().__init__()
        self.tracks = list(tracks)
        self.audio_converter = audio_converter
        self._cancelled = False

    def cancel(self):
        self._cancelled = True

    @property
    def was_cancelled(self) -> bool:
        return self._cancelled

    def run(self):
        for i, track in enumerate(self.tracks, start=1):
            if self._cancelled:
                break
            self.progress.emit(i, len(self.tracks), track.filename)
            try:
                result = self.audio_converter.measure_loudness(track.filepath)
                self.measured.emit(track, result.integrated, "")
            except Exception as e:
                log.warning("Lautheitsmessung fehlgeschlagen (%s): %s", track.filename, e)
                self.measured.emit(track, None, str(e))
        self.completed.emit()


class LoudnessApplyWorker(QThread):
    """Gleicht die Lautstaerke der uebergebenen Tracks an. Jede Datei wird zuerst
    in eine temporaere Datei im selben Ordner geschrieben und ersetzt das Original
    erst bei Erfolg - ein Fehler oder Abbruch laesst die Originaldatei unberuehrt."""
    progress = pyqtSignal(int, int, str)  # aktueller Index, Gesamtzahl, Dateiname
    error = pyqtSignal(str, str)  # Dateiname, Fehlermeldung
    completed = pyqtSignal(int)  # Anzahl angepasster Tracks

    def __init__(self, items: list, audio_converter: AudioConverter):
        super().__init__()
        self.items = list(items)  # [(Track, Verstaerkung in dB)]
        self.audio_converter = audio_converter
        self._cancelled = False

    def cancel(self):
        self._cancelled = True

    @staticmethod
    def _source_bitrate(filepath: str) -> Optional[int]:
        try:
            from mutagen.mp3 import MP3
            return int(MP3(filepath).info.bitrate)
        except Exception:
            return None

    def run(self):
        done = 0
        for i, (track, gain_db) in enumerate(self.items, start=1):
            if self._cancelled:
                break
            self.progress.emit(i, len(self.items), track.filename)

            source = Path(track.filepath)
            temp = source.with_name(f".tmp_norm_{source.name}")
            try:
                bitrate = loudness.pick_bitrate_kbps(self._source_bitrate(track.filepath))
                self.audio_converter.apply_gain(str(source), str(temp), gain_db, bitrate)
                replace_with_retry(temp, source)
                done += 1
            except Exception as e:
                log.warning("Lautstaerke-Anpassung fehlgeschlagen (%s): %s", track.filename, e)
                self.error.emit(track.filename, str(e))
            finally:
                try:
                    temp.unlink(missing_ok=True)
                except OSError:
                    pass
        self.completed.emit(done)


class RfidPoller(QThread):
    """Fragt Reader- und Kartenstatus regelmaessig im Hintergrund ab - die
    PC/SC-Aufrufe (Transmit, Reconnect) koennen sonst den GUI-Thread
    spuerbar blockieren."""
    status_changed = pyqtSignal(object)  # RfidStatus

    def __init__(self, reader: RFIDReader, interval_ms: int = 500):
        super().__init__()
        self.reader = reader
        self.interval_ms = interval_ms
        self.paused = False  # z.B. im TonUINO-seriell-Modus
        self._stopped = False

    def stop(self):
        self._stopped = True

    def run(self):
        while not self._stopped:
            if not self.paused:
                try:
                    self.status_changed.emit(self.reader.poll())
                except Exception as e:
                    self.status_changed.emit(RfidStatus(reader="error", error=str(e)))
            self.msleep(self.interval_ms)


class TonuinoConnectWorker(QThread):
    """Oeffnet die serielle Verbindung zum TonUINO - der Arduino resettet dabei
    und die Verbindung wartet ca. 2 s, was im GUI-Thread die Oberflaeche einfriert."""
    completed = pyqtSignal(bool, str)  # Erfolg, Fehlermeldung

    def __init__(self, tonuino: TonuinoSerial, port: str):
        super().__init__()
        self.tonuino = tonuino
        self.port = port

    def run(self):
        try:
            self.tonuino.connect(self.port)
            self.completed.emit(True, "")
        except TonuinoSerialError as e:
            self.completed.emit(False, str(e))
        except Exception as e:
            self.completed.emit(False, f"Unerwarteter Fehler: {e}")


class FolderNameWorker(QThread):
    """Ermittelt die Ordnernamen (Album-Tag des ersten getaggten Tracks) im
    Hintergrund - auf einer SD-Karte mit vielen untaggten Ordnern waeren das
    sonst tausende Dateizugriffe im GUI-Thread direkt nach dem Oeffnen."""
    name_found = pyqtSignal(int, str)  # Ordnernummer, Name
    completed = pyqtSignal()

    def __init__(self, folders: list, metadata_manager: MetadataManager):
        super().__init__()
        self.folders = folders
        self.metadata_manager = metadata_manager
        self._cancelled = False

    def cancel(self):
        self._cancelled = True

    def run(self):
        for folder in self.folders:
            for position, track in enumerate(sorted(folder.tracks, key=lambda t: t.index)):
                if self._cancelled:
                    return
                metadata = self.metadata_manager.read_metadata(track.filepath)
                # Ein manuell vergebener Name steht in allen Tracks - der erste genuegt
                name = (metadata.folder_name if position == 0 else "") or metadata.album
                if name:
                    self.name_found.emit(folder.index, name)
                    break
        self.completed.emit()


class TrackMetaWorker(QThread):
    """Liest die Metadaten der Tracks eines Ordners genau einmal pro Datei
    (Tags, Dauer und - nur vom ersten Track mit Cover - das Cover)."""
    track_loaded = pyqtSignal(int, int, object, object)  # Generation, Zeile, TrackMetadata, Cover-Bytes|None
    completed = pyqtSignal(int)  # Generation

    def __init__(self, generation: int, tracks: list, metadata_manager: MetadataManager):
        super().__init__()
        self.generation = generation
        self.tracks = tracks
        self.metadata_manager = metadata_manager
        self._cancelled = False

    def cancel(self):
        self._cancelled = True

    def run(self):
        cover_sent = False
        for row, track in enumerate(self.tracks):
            if self._cancelled:
                return
            metadata, cover = self.metadata_manager.read_metadata_and_cover(track.filepath)
            if cover_sent:
                cover = None
            elif cover:
                cover_sent = True
            self.track_loaded.emit(self.generation, row, metadata, cover)
        self.completed.emit(self.generation)


class PurgeWorker(QThread):
    """Thread fuer das Bereinigen der SD-Karte (Whitelist-Purge), damit die
    UI waehrend der Dateisystem-Operationen nicht einfriert."""
    completed = pyqtSignal(bool, str)  # Erfolg, Fehlermeldung (leer bei Erfolg)

    def __init__(self, sd_card: SDCard):
        super().__init__()
        self.sd_card = sd_card

    def run(self):
        try:
            self.sd_card.purge()
            self.completed.emit(True, "")
        except Exception as e:
            self.completed.emit(False, str(e))


class TonuinoCardWorker(QThread):
    """Fuehrt eine Karten-Programmierung ueber den seriell verbundenen TonUINO aus
    (WRITECARD-Befehl), damit die UI waehrend der - potenziell langen, da die
    Firmware ohne eigenes Timeout auf das Auflegen der Karte wartet - Antwort
    nicht einfriert."""
    completed = pyqtSignal(bool, str)  # Erfolg, Meldungstext (Firmware-Text bzw. Fehler)

    def __init__(self, tonuino: TonuinoSerial, mode: int,
                 folder: Optional[int] = None, special: Optional[int] = None,
                 special2: Optional[int] = None):
        super().__init__()
        self.tonuino = tonuino
        self.mode = mode
        self.folder = folder
        self.special = special
        self.special2 = special2

    def run(self):
        try:
            message = self.tonuino.write_card(
                self.mode, folder=self.folder, special=self.special, special2=self.special2
            )
            self.completed.emit(True, message)
        except TonuinoWriteCancelled as e:
            self.completed.emit(False, str(e))
        except (TonuinoSerialError, ValueError) as e:
            self.completed.emit(False, str(e))
        except Exception as e:
            self.completed.emit(False, f"Unerwarteter Fehler: {e}")


class CoverResearchWorker(QThread):
    """Recherche fuer ein Kartencover (Tags lesen, MusicBrainz, Gemini-Suche)"""
    completed = pyqtSignal(object, object)  # FolderInfo, CoverBrief
    failed = pyqtSignal(str)

    def __init__(self, folder: Folder, metadata_manager: MetadataManager,
                 client, text_model: str, extra: str = "", online_cover: bool = True):
        super().__init__()
        self.folder = folder
        self.metadata_manager = metadata_manager
        self.client = client
        self.text_model = text_model
        self.extra = extra
        self.online_cover = online_cover

    def run(self):
        from core.cover_research import gather_folder_info, research
        try:
            info = gather_folder_info(self.folder, self.metadata_manager)
            brief = research(info, self.client, self.text_model, self.extra, online_cover=self.online_cover)
            self.completed.emit(info, brief)
        except Exception as e:
            log.exception("Cover-Recherche fehlgeschlagen")
            self.failed.emit(str(e))


class CoverGenerateWorker(QThread):
    """Erzeugt nacheinander count Motiv-Varianten (Gemini bzw. Ambient Blur)"""
    variant_ready = pyqtSignal(object)  # PIL.Image
    failed = pyqtSignal(str)
    completed = pyqtSignal()

    def __init__(self, source: str, request, client, image_model: str, count: int = 1):
        super().__init__()
        self.source = source
        self.request = request
        self.client = client
        self.image_model = image_model
        self.count = count
        self._cancelled = False

    def cancel(self):
        self._cancelled = True

    def run(self):
        from core.cover_generator import generate_art
        for _ in range(self.count):
            if self._cancelled:
                break
            try:
                self.variant_ready.emit(generate_art(self.source, self.request, self.client, self.image_model))
            except Exception as e:
                log.exception("Cover-Generierung fehlgeschlagen")
                self.failed.emit(str(e))
                break
        self.completed.emit()


class CoverBatchWorker(QThread):
    """Erzeugt fuer mehrere Ordner je ein Kartencover und speichert es direkt
    (Recherche -> eine Variante -> card.png/card.json im Ordner).
    Karten aus dem ESPuino-Mediahub (core/mediahub_cards.MediahubFolder, ohne
    Nummer) haben eine feste Album-ID, ihre Kartendateien liegen im Archiv, und
    die Dateien der Mediathek werden nie getaggt."""
    progress = pyqtSignal(int, int, str)  # aktueller Index, Gesamtzahl, Ordnername
    folder_done = pyqtSignal(object)  # Ordnernummer bzw. der Folder ohne Nummer
    error = pyqtSignal(str, str)  # Ordner, Fehlermeldung
    completed = pyqtSignal(int)  # Anzahl erfolgreich

    def __init__(self, folders: list, metadata_manager: MetadataManager, client,
                 text_model: str, image_model: str, source: str, style: str, orientation: str,
                 archive=None, series_profile=None, anchor_images=None):
        super().__init__()
        self.archive = archive  # optional: Ergebnisse zusaetzlich im Cover-Archiv ablegen
        # optional: alle Ordner gehoeren zu dieser Serie (core/series.py) - Anker
        # und Stilbibel gehen mit, die Alben werden der Serie zugeordnet
        self.series_profile = series_profile
        self.anchor_images = list(anchor_images or [])
        if series_profile is not None and series_profile.style:
            style = series_profile.style
        self.folders = folders
        self.metadata_manager = metadata_manager
        self.client = client
        self.text_model = text_model
        self.image_model = image_model
        self.source = source
        self.style = style
        self.orientation = orientation
        self._cancelled = False

    def cancel(self):
        self._cancelled = True

    def run(self):
        from core import album_identity, card_store
        from core.card_text import derive_card_text
        from core.cover_archive import ROLE_CARD_COVER
        from core.cover_generator import GenerationRequest, generate_art
        from core.cover_research import gather_folder_info, research

        done = 0
        for i, folder in enumerate(self.folders, start=1):
            if self._cancelled:
                break
            fixed_album_id = getattr(folder, "album_id", "")  # Mediahub-Karte
            card_dir = getattr(folder, "card_dir", "") or folder.path
            label = f"Ordner {folder.index:02d}" if folder.index is not None else (folder.name or "Karte")
            self.progress.emit(i, len(self.folders), label)
            try:
                info = gather_folder_info(folder, self.metadata_manager)
                brief = research(info, self.client, self.text_model)
                request = GenerationRequest(info=info, brief=brief, style=self.style, orientation=self.orientation)
                if self.series_profile is not None:
                    request.series_name = self.series_profile.name
                    request.series_bible = self.series_profile.style_bible
                    request.anchor_images = list(self.anchor_images)
                art = generate_art(self.source, request, self.client, self.image_model)
                series, title = derive_card_text(info, brief)
                if fixed_album_id and folder.name:
                    # der im Mediahub vergebene Kartenname; Tags gehoeren bei Wiedergabelisten zu fremden Alben
                    series, title = "", folder.name
                meta = card_store.CardMeta(
                    title=title or info.album or label, series=series, orientation=self.orientation,
                    source=self.source,
                    style=self.style, model=self.image_model if self.source == "ai" else "",
                    research_sources=brief.sources)
                if self.archive is not None:
                    paths = [t.filepath for t in folder.tracks]
                    album_id = fixed_album_id or album_identity.read_album_id(paths)
                    if not album_id:
                        album_id = album_identity.new_album_id()
                        album_identity.write_album_id(paths, album_id)
                    self.archive.ensure_album(album_id, label if fixed_album_id else (info.album or label),
                                              info.artist, path="" if fixed_album_id else folder.path)
                    if self.series_profile is not None:
                        self.archive.assign_album(album_id, self.series_profile.id)
                    entry = self.archive.add_cover(album_id, art, self.source, style=self.style,
                                                   model=meta.model)
                    self.archive.set_role(album_id, entry.id, ROLE_CARD_COVER)
                    meta.album_id, meta.archive_cover_id = album_id, entry.id
                Path(card_dir).mkdir(parents=True, exist_ok=True)
                card_store.save_card(card_dir, art, meta, folder.index)
                done += 1
                self.folder_done.emit(folder.index if folder.index is not None else folder)
            except Exception as e:
                log.exception("Batch-Cover fuer %s fehlgeschlagen", label)
                self.error.emit(label, getattr(e, "short", str(e)))  # Tipps nur einmal, siehe Hauptfenster
        self.completed.emit(done)


class CoverExtendWorker(QThread):
    """Erweitert ein Bild per Bildmodell auf das Kartenformat (Outpainting). Mit masking geht
    es abgedeckt an Google, und die abgedeckten Stellen kommen danach aus dem Original zurueck."""
    done = pyqtSignal(object)  # PIL.Image
    doubtful = pyqtSignal()  # vor done: Motiv weicht stark ab oder harte Naht am Rand des Originals
    failed = pyqtSignal(str)
    refused = pyqtSignal(str)  # von Google abgelehnt: hier hilft das Abdecken

    def __init__(self, client, image_model: str, image_bytes: bytes, orientation: str, masking=None):
        super().__init__()
        self.client = client
        self.image_model = image_model
        self.image_bytes = image_bytes
        self.orientation = orientation
        self.masking = masking

    def run(self):
        from core.cover_generator import ImageRefused, check_extension, extend_masked, extend_to_card
        try:
            if self.masking is not None:
                result = extend_masked(self.client, self.image_model, self.image_bytes, self.orientation,
                                       self.masking)
            else:
                image = extend_to_card(self.client, self.image_model, self.image_bytes, self.orientation)
                result = check_extension(image, self.image_bytes)
            if result.doubtful:
                self.doubtful.emit()
            self.done.emit(result.image)
        except ImageRefused as e:
            log.info("KI-Randergaenzung abgelehnt")
            self.refused.emit(str(e))
        except Exception as e:
            log.exception("KI-Randergaenzung fehlgeschlagen")
            self.failed.emit(str(e))


class CoverBackGenerateWorker(QThread):
    """Erzeugt ein Rueckseiten-Motiv passend zur Vorderseite (Bildmodell); mit masking geht die
    Vorderseite abgedeckt an Google"""
    done = pyqtSignal(object)  # PIL.Image
    failed = pyqtSignal(str)
    refused = pyqtSignal(str)  # von Google abgelehnt: hier hilft das Abdecken der Vorderseite

    def __init__(self, client, image_model: str, request, front_bytes: bytes, wish: str, with_tracklist: bool,
                 masking=None):
        super().__init__()
        self.masking = masking
        self.client = client
        self.image_model = image_model
        self.request = request
        self.front_bytes = front_bytes
        self.wish = wish
        self.with_tracklist = with_tracklist

    def run(self):
        from core.cover_generator import ImageRefused, generate_back_art
        try:
            self.done.emit(generate_back_art(self.client, self.image_model, self.request, self.front_bytes,
                                             self.wish, self.with_tracklist, masking=self.masking))
        except ImageRefused as e:
            log.info("KI-Rueckseite abgelehnt")
            self.refused.emit(str(e))
        except Exception as e:
            log.exception("KI-Rueckseite fehlgeschlagen")
            self.failed.emit(str(e))


class BackIdeasWorker(QThread):
    """Drei Rueckseiten-Ideen vom Textmodell"""
    done = pyqtSignal(object)  # list[str]
    failed = pyqtSignal(str)

    def __init__(self, client, text_model: str, request, front_bytes=None):
        super().__init__()
        self.client = client
        self.text_model = text_model
        self.request = request
        self.front_bytes = front_bytes
        self.front_description = ""

    def run(self):
        from core.cover_generator import suggest_back_ideas
        try:
            ideas = suggest_back_ideas(self.client, self.text_model, self.request, self.front_bytes)
            self.front_description = suggest_back_ideas.last_front_description
            self.done.emit(ideas)
        except Exception as e:
            log.exception("Rueckseiten-Ideen fehlgeschlagen")
            self.failed.emit(str(e))


# --- Serien (core/series.py) ---------------------------------------------------------------------

class SeriesDetectWorker(QThread):
    """Reihe eines Ordners erkennen: MusicBrainz-Serienrelation (falls die
    Tags eine Release-Group-ID tragen), sonst die Reihe aus den Tags"""
    done = pyqtSignal(str, str)  # Name, MusicBrainz-Serien-ID

    def __init__(self, info, release_group_id: str = ""):
        super().__init__()
        self.info = info
        self.release_group_id = release_group_id

    def run(self):
        from core.series import detect_series
        try:
            name, mb_id = detect_series(self.info, self.release_group_id)
        except Exception:
            log.exception("Serien-Erkennung fehlgeschlagen")
            name, mb_id = "", ""
        self.done.emit(name, mb_id)


class StyleBibleWorker(QThread):
    """Stilbibel vom Textmodell (Anker-Bilder + Recherche)"""
    done = pyqtSignal(str)
    failed = pyqtSignal(str)

    def __init__(self, client, text_model: str, name: str, anchor_images: list, research: str):
        super().__init__()
        self.client = client
        self.text_model = text_model
        self.name = name
        self.anchor_images = anchor_images
        self.research = research

    def run(self):
        from core.series import suggest_style_bible
        try:
            self.done.emit(suggest_style_bible(self.client, self.text_model, self.name, self.anchor_images,
                                               self.research))
        except Exception as e:
            log.exception("Stilbibel fehlgeschlagen")
            self.failed.emit(str(e))


class CharacterSheetWorker(QThread):
    """Charakterblatt der Reihe vom Bildmodell (Anker als Referenz)"""
    done = pyqtSignal(object)  # PIL.Image
    failed = pyqtSignal(str)

    def __init__(self, client, image_model: str, name: str, style: str, style_bible: str, anchor_images: list):
        super().__init__()
        self.client = client
        self.image_model = image_model
        self.name = name
        self.style = style
        self.style_bible = style_bible
        self.anchor_images = anchor_images

    def run(self):
        from core.series import generate_character_sheet
        try:
            self.done.emit(generate_character_sheet(self.client, self.image_model, self.name, self.style,
                                                    self.style_bible, self.anchor_images))
        except Exception as e:
            log.exception("Charakterblatt fehlgeschlagen")
            self.failed.emit(str(e))


# --- Bibliothek (core/library.py) -----------------------------------------------------------------

class LibraryScanWorker(QThread):
    """Liest die Bibliothek-Ordner ein (inkrementell), gleicht sie mit dem
    Cover-Archiv ab, speichert den Index und ermittelt, welche Alben schon
    auf der SD-Karte liegen (Album-ID -> Ordnernummer)."""
    progress = pyqtSignal(int, int, str)  # aktueller Index, Gesamtzahl, Albumname
    completed = pyqtSignal(object, object)  # LibraryIndex, dict Album-ID -> Ordnernummer
    failed = pyqtSignal(str)

    def __init__(self, library, previous, archive=None, sd_folders=None, rescan: bool = True):
        super().__init__()
        self.library = library
        self.previous = previous
        self.archive = archive
        self.sd_folders = list(sd_folders or [])
        self.rescan = rescan
        self._cancelled = False

    def cancel(self):
        self._cancelled = True

    def run(self):
        from core.library import sd_album_ids
        try:
            index = self.previous
            if self.rescan and self.library is not None:
                index = self.library.scan(self.previous, self.progress.emit, lambda: self._cancelled)
                # Abgebrochen: das Ergebnis ist unvollstaendig - weder Archiv abgleichen
                # (wuerde Bibliothekspfade fuer verwaist halten) noch Index speichern
                if not self._cancelled:
                    if self.archive is not None:
                        index = self.library.match(index, self.archive)
                    self.library.save(index)
            sd_map = sd_album_ids(self.sd_folders) if self.sd_folders else {}
            self.completed.emit(index, sd_map)
        except Exception as e:
            log.exception("Bibliothek einlesen fehlgeschlagen")
            self.failed.emit(str(e))


class AlbumTracksWorker(QThread):
    """Liest die Tracks eines Bibliotheksalbums (Nummer, Titel, Dauer) im Hintergrund"""
    done = pyqtSignal(str, object)  # Albumpfad, list[LibraryTrack]

    def __init__(self, album):
        super().__init__()
        self.album = album
        self._cancelled = False

    def cancel(self):
        self._cancelled = True

    def run(self):
        from core.library import read_album_tracks
        try:
            tracks = read_album_tracks(self.album, cancelled=lambda: self._cancelled)
        except Exception:
            log.exception("Album-Tracks nicht lesbar")
            tracks = []
        if not self._cancelled:
            self.done.emit(self.album.path, tracks)


class CoverProposalWorker(QThread):
    """Cover-Stapelsuche: Vorschlaege fuer alle Bibliotheksalben ohne Cover"""
    progress = pyqtSignal(int, int, str)
    done = pyqtSignal(object)  # list[AlbumProposal]
    failed = pyqtSignal(str)

    def __init__(self, index, archive, sources: list):
        super().__init__()
        self.index = index
        self.archive = archive
        self.sources = list(sources)
        self._cancelled = False

    def cancel(self):
        self._cancelled = True

    def run(self):
        from core.cover_batch import propose_all
        try:
            proposals = propose_all(self.index, self.archive, self.sources, progress=self.progress.emit,
                                    cancelled=lambda: self._cancelled)
            if not self._cancelled:
                self.done.emit(proposals)
        except Exception as e:
            log.exception("Cover-Stapelsuche fehlgeschlagen")
            self.failed.emit(str(e))


class RecognizeWorker(QThread):
    """Stuecke per fpcalc + AcoustID erkennen; danach die Releases der
    gemeinsamen Release-Group (MusicBrainz) als Kandidaten laden"""
    progress = pyqtSignal(int, int, str)
    done = pyqtSignal(object, object)  # RecognitionResult, list[ReleaseCandidate]
    failed = pyqtSignal(str)

    def __init__(self, paths: list, api_key: str, fpcalc: str):
        super().__init__()
        self.paths = paths
        self.api_key = api_key
        self.fpcalc = fpcalc
        self._cancelled = False

    def cancel(self):
        self._cancelled = True

    def run(self):
        from core import tag_lookup
        from core.acoustid import recognize_files
        try:
            result = recognize_files(self.paths, self.api_key, self.fpcalc, progress=self.progress.emit,
                                     cancelled=lambda: self._cancelled)
            candidates = []
            if result.release_group_id and not self._cancelled:
                try:
                    candidates = tag_lookup.candidates_from_release_group(result.release_group_id)
                except Exception as e:
                    log.warning("Releases der Release-Group nicht ladbar: %s", e)
            if not self._cancelled:
                self.done.emit(result, candidates)
        except Exception as e:
            log.exception("Erkennung fehlgeschlagen")
            self.failed.emit(str(e))
