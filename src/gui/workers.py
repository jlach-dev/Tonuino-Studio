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
        for i, filepath in enumerate(self.filepaths, start=1):
            if self._cancelled:
                break

            self.progress.emit(i, len(self.filepaths), os.path.basename(filepath))

            next_number = 1
            while next_number in self._used_numbers:
                next_number += 1
            if next_number > MAX_TRACKS_PER_FOLDER:
                break

            dest_filename = f"{next_number:03d}.mp3"
            dest_path = Path(self.folder.path) / dest_filename

            try:
                if self.audio_converter.needs_conversion(filepath):
                    if not self.audio_converter.is_available:
                        self.error.emit(os.path.basename(filepath), "FFmpeg ist nicht verfügbar.")
                        continue
                    self.audio_converter.convert_to_mp3(filepath, str(dest_path))
                else:
                    shutil.copy2(filepath, dest_path)

                # Tags stehen jetzt in der Zieldatei (bei Konvertierung von FFmpeg
                # uebernommen, bei MP3 mitkopiert) - dort lesen statt in der Quelle,
                # die ggf. kein MP3 ist und von mutagen.mp3 nicht gelesen werden kann.
                metadata = self.metadata_manager.read_metadata(str(dest_path))
                if metadata.title:
                    self.metadata_manager.write_metadata(str(dest_path), track_number=next_number)

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
                self.error.emit(os.path.basename(filepath), str(e))

        self.completed.emit(added)


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
            for track in sorted(folder.tracks, key=lambda t: t.index):
                if self._cancelled:
                    return
                album = self.metadata_manager.read_metadata(track.filepath).album
                if album:
                    self.name_found.emit(folder.index, album)
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
    (Recherche -> eine Variante -> card.png/card.json im Ordner)."""
    progress = pyqtSignal(int, int, str)  # aktueller Index, Gesamtzahl, Ordnername
    folder_done = pyqtSignal(int)  # Ordnernummer
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
        from core.card_layout import prepare_card_art
        from core.card_text import derive_card_text
        from core.cover_archive import ROLE_CARD_COVER
        from core.cover_generator import GenerationRequest, generate_art
        from core.cover_research import gather_folder_info, research

        done = 0
        for i, folder in enumerate(self.folders, start=1):
            if self._cancelled:
                break
            label = f"Ordner {folder.index:02d}"
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
                meta = card_store.CardMeta(
                    title=title or info.album or label, series=series, orientation=self.orientation,
                    source=self.source,
                    style=self.style, model=self.image_model if self.source == "ai" else "",
                    research_sources=brief.sources)
                if self.archive is not None:
                    paths = [t.filepath for t in folder.tracks]
                    album_id = album_identity.read_album_id(paths)
                    if not album_id:
                        album_id = album_identity.new_album_id()
                        album_identity.write_album_id(paths, album_id)
                    self.archive.ensure_album(album_id, info.album or label, info.artist, path=folder.path)
                    if self.series_profile is not None:
                        self.archive.assign_album(album_id, self.series_profile.id)
                    entry = self.archive.add_cover(album_id, art, self.source, style=self.style,
                                                   model=meta.model)
                    self.archive.set_role(album_id, entry.id, ROLE_CARD_COVER)
                    meta.album_id, meta.archive_cover_id = album_id, entry.id
                card_store.save_card(folder.path, prepare_card_art(art, self.orientation), meta, folder.index)
                done += 1
                self.folder_done.emit(folder.index)
            except Exception as e:
                log.exception("Batch-Cover fuer %s fehlgeschlagen", label)
                self.error.emit(label, str(e))
        self.completed.emit(done)


class CoverExtendWorker(QThread):
    """Erweitert ein Bild per Bildmodell auf das Kartenformat (Outpainting)"""
    done = pyqtSignal(object)  # PIL.Image
    failed = pyqtSignal(str)

    def __init__(self, client, image_model: str, image_bytes: bytes, orientation: str):
        super().__init__()
        self.client = client
        self.image_model = image_model
        self.image_bytes = image_bytes
        self.orientation = orientation

    def run(self):
        from core.cover_generator import extend_to_card
        try:
            self.done.emit(extend_to_card(self.client, self.image_model, self.image_bytes, self.orientation))
        except Exception as e:
            log.exception("KI-Randergaenzung fehlgeschlagen")
            self.failed.emit(str(e))


class CoverBackGenerateWorker(QThread):
    """Erzeugt ein Rueckseiten-Motiv passend zur Vorderseite (Bildmodell)"""
    done = pyqtSignal(object)  # PIL.Image
    failed = pyqtSignal(str)

    def __init__(self, client, image_model: str, request, front_bytes: bytes, wish: str, with_tracklist: bool):
        super().__init__()
        self.client = client
        self.image_model = image_model
        self.request = request
        self.front_bytes = front_bytes
        self.wish = wish
        self.with_tracklist = with_tracklist

    def run(self):
        from core.cover_generator import generate_back_art
        try:
            self.done.emit(generate_back_art(self.client, self.image_model, self.request, self.front_bytes,
                                             self.wish, self.with_tracklist))
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
                if self.archive is not None:
                    index = self.library.match(index, self.archive)
                if not self._cancelled:
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
