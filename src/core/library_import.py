"""
Album aus der Bibliothek auf die SD-Karte bringen (Drag & Drop)

Ablauf (siehe import_album / finish_import):
  1. Tracks kopieren bzw. nach MP3 konvertieren und durchnummerieren
     (gui/workers.py TrackAddWorker - im Hintergrund oder synchron)
  2. Album-ID: aus den Bibliothekstags, sonst aus dem Archiv (Fingerabdruck),
     sonst neu; in die SD-Kopien schreiben, in die Bibliotheksdateien nur mit
     Erlaubnis (QSettings "library/write_id_tags")
  3. Album im Archiv anlegen/verknuepfen (Titel, Interpret, SD- und Bibliothekspfad)
  4. Album-Cover aus dem Archiv in die SD-Tracks einbetten, falls sie keins haben
  5. Hat das Archiv ein Kartencover (Rolle ROLE_CARD_COVER), card.png/card.json
     (und die Rueckseite) in den SD-Ordner schreiben - wie der Cover-Manager
"""

import logging
from dataclasses import dataclass, field
from typing import Callable, List, Optional

from core import album_identity, card_store
from core.card_layout import BACK_MODE_IMAGE, ORIENTATION_PORTRAIT, prepare_card_art
from core.cover_archive import ROLE_ALBUM_COVER, ROLE_CARD_COVER, CoverArchive
from core.library import LibraryAlbum, LibraryIndex, read_album, write_album_cover, write_album_id_to_album
from core.sd_card import Folder, SDCard, Track

log = logging.getLogger(__name__)

MODE_APPEND = "append"
MODE_REPLACE = "replace"


@dataclass
class ImportResult:
    album_id: str = ""
    added: int = 0
    library_tagged: int = 0  # Bibliotheksdateien, die die Album-ID neu bekommen haben
    cover_embedded: int = 0  # SD-Tracks, die ein Album-Cover aus dem Archiv bekommen haben
    card_written: bool = False
    back_written: bool = False
    errors: List[str] = field(default_factory=list)

    @property
    def summary(self) -> str:
        parts = [f"{self.added} Track(s) kopiert"]
        if self.cover_embedded:
            parts.append("Album-Cover eingebettet")
        if self.card_written:
            parts.append("Kartencover übernommen" + (" (mit Rückseite)" if self.back_written else ""))
        if self.library_tagged:
            parts.append(f"Album-ID in {self.library_tagged} Bibliotheksdatei(en) geschrieben")
        return ", ".join(parts)


def library_album_for(path: str, index=None) -> LibraryAlbum:
    """Album aus dem Index, sonst frisch eingelesen (z.B. ein aus dem Explorer gezogener Ordner)"""
    album = index.find(path) if index is not None else None
    if album is not None and album.tracks:
        return album
    return read_album(path)


def clear_folder_tracks(sd_card: SDCard, folder: Folder):
    """Ersetzen: alle Tracks des Ordners loeschen (Kartencover-Dateien bleiben)"""
    if folder.tracks:
        sd_card.delete_tracks(folder, list(folder.tracks))


def resolve_album_id(album: LibraryAlbum, archive: Optional[CoverArchive]) -> str:
    """Album-ID des Bibliotheksalbums: Tag, sonst Archiv-Treffer ueber Fingerabdruck
    oder Titel/Interpret, sonst neu"""
    if album.album_id:
        return album.album_id
    if archive is not None:
        if album.fingerprint:
            for record in archive.all_albums():
                if record.fingerprint and record.fingerprint == album.fingerprint:
                    return record.id
        record = archive.find_album(album.title, album.artist) if album.title else None
        if record is not None:
            return record.id
    return album_identity.new_album_id()


def _first_cover_bytes(tracks: List[Track], metadata_manager) -> Optional[bytes]:
    for track in tracks[:3]:
        cover = metadata_manager.get_cover_bytes(track.filepath)
        if cover:
            return cover
    return None


def _embed_album_cover(folder: Folder, album_id: str, archive: CoverArchive, metadata_manager) -> int:
    if not folder.tracks or _first_cover_bytes(folder.tracks, metadata_manager):
        return 0
    entry = next((e for e in archive.covers(album_id) if ROLE_ALBUM_COVER in e.roles), None)
    if entry is None:
        return 0
    try:
        data = archive.load_bytes(album_id, entry)
    except OSError:
        return 0
    mime = "image/png" if entry.file.lower().endswith(".png") else "image/jpeg"
    return sum(1 for t in folder.tracks if metadata_manager.set_cover_bytes(t.filepath, data, mime))


def _write_card(folder: Folder, album: LibraryAlbum, album_id: str, archive: CoverArchive,
                metadata_manager, orientation: str, result: ImportResult):
    """card.png/card.json (+ Rueckseite) aus dem Kartencover des Archivs"""
    entry = next((e for e in archive.covers(album_id) if ROLE_CARD_COVER in e.roles), None)
    if entry is None:
        return
    from core.card_text import derive_card_text
    from core.cover_research import gather_folder_info
    info = gather_folder_info(folder, metadata_manager, max_titles=25)
    series, title = derive_card_text(info)
    # Gestaltung einer frueheren Karte dieses Ordners bzw. der im Cover-Manager
    # aus der Bibliothek heraus gespeicherten Karte behalten
    existing = card_store.load_meta(folder.path) or card_store.load_meta(str(archive.card_dir(album_id)))
    meta = card_store.CardMeta(
        title=title or album.name, series=series, orientation=orientation,
        source=entry.kind, style=entry.style, prompt=entry.prompt, model=entry.model,
        album_id=album_id, archive_cover_id=entry.id)
    if existing is not None:
        # Gestaltung einer frueheren Karte dieses Ordners behalten
        meta.orientation, meta.show_number = existing.orientation, existing.show_number
        meta.title_style, meta.series_style = existing.title_style, existing.series_style
    art = prepare_card_art(archive.load_image(album_id, entry), meta.orientation)
    card_store.save_card(folder.path, art, meta, folder.index)
    folder.card_path = str(card_store.card_paths(folder.path)[0])
    result.card_written = True
    back = archive.load_back_bytes(album_id, entry)
    if back:
        import io
        from PIL import Image
        with Image.open(io.BytesIO(back)) as img:
            img.load()
            back_art = prepare_card_art(img.convert("RGB"), meta.orientation)
        meta.back_mode, meta.back_archive_cover_id = BACK_MODE_IMAGE, entry.id
        card_store.save_back(folder.path, back_art, meta)
        result.back_written = True
    else:
        card_store.delete_back(folder.path)


def archive_album_cover(archive: CoverArchive, album_id: str) -> Optional[bytes]:
    """Das im Archiv als Album-Cover markierte Bild (Rohdaten) oder None"""
    entry = next((e for e in archive.covers(album_id) if ROLE_ALBUM_COVER in e.roles), None)
    if entry is None:
        return None
    try:
        return archive.load_bytes(album_id, entry)
    except OSError:
        return None


@dataclass
class EmbedResult:
    albums: int = 0  # Alben, die ein Cover bekommen haben
    tracks: int = 0  # Dateien mit eingebettetem Cover
    skipped: int = 0  # Alben ohne Cover, fuer die das Archiv keins hat


def embed_archive_covers(index: LibraryIndex, archive: CoverArchive,
                         progress: Optional[Callable[[int, int, str], None]] = None,
                         cancelled: Optional[Callable[[], bool]] = None) -> EmbedResult:
    """Alle Bibliotheksalben ohne Cover: das im Archiv gesetzte Album-Cover in die
    Dateien einbetten und als cover.jpg ablegen (Stapellauf)"""
    from core.card_layout import album_cover_jpeg
    from PIL import Image
    import io
    result = EmbedResult()
    todo = [a for a in index.albums if not a.offline and not a.has_cover and a.album_id and a.tracks]
    for i, album in enumerate(todo, start=1):
        if cancelled is not None and cancelled():
            break
        if progress is not None:
            progress(i, len(todo), album.name)
        data = archive_album_cover(archive, album.album_id)
        if data is None:
            result.skipped += 1
            continue
        try:
            with Image.open(io.BytesIO(data)) as img:
                img.load()
                jpeg = album_cover_jpeg(img)
        except Exception as e:
            log.warning("Archiv-Cover von %s nicht lesbar: %s", album.name, e)
            result.skipped += 1
            continue
        written = write_album_cover(list(album.tracks), album.path, jpeg)
        if written:
            result.albums += 1
            result.tracks += written
            album.has_cover = True
        else:
            result.skipped += 1
    return result


def finish_import(sd_card: SDCard, folder: Folder, album: LibraryAlbum, archive: Optional[CoverArchive],
                  metadata_manager, write_library_tags: bool, orientation: str = ORIENTATION_PORTRAIT,
                  result: Optional[ImportResult] = None) -> ImportResult:
    """Schritte 2-5 nach dem Kopieren (siehe Modul-Doku). folder.tracks muss die
    kopierten Tracks enthalten."""
    result = result or ImportResult()
    try:
        sd_card.close_track_gaps(folder)
    except OSError as e:
        result.errors.append(f"Nummerierung: {e}")

    album_id = resolve_album_id(album, archive)
    result.album_id = album_id
    sd_paths = [t.filepath for t in folder.tracks]
    if sd_paths:
        album_identity.write_album_id(sd_paths, album_id)
    if write_library_tags and not album.album_id:
        result.library_tagged = write_album_id_to_album(album, album_id)

    if archive is None:
        return result
    try:
        record = archive.ensure_album(album_id, album.title or album.name, album.artist, path=folder.path)
        changed = False
        if album.path not in record.paths:
            record.paths.append(album.path)
            changed = True
        if album.fingerprint and record.fingerprint != album.fingerprint:
            record.fingerprint, changed = album.fingerprint, True
        if album.mb_release_group_id and not record.mb_release_group_id:
            record.mb_release_group_id, changed = album.mb_release_group_id, True
        if album.mb_release_id and not record.mb_release_id:
            record.mb_release_id, changed = album.mb_release_id, True
        if changed:
            archive.save_album(record)
        result.cover_embedded = _embed_album_cover(folder, album_id, archive, metadata_manager)
        _write_card(folder, album, album_id, archive, metadata_manager, orientation, result)
    except Exception as e:
        log.exception("Archiv-Verknuepfung nach dem Import fehlgeschlagen")
        result.errors.append(f"Archiv: {e}")
    return result


def import_album(sd_card: SDCard, folder: Folder, album: LibraryAlbum, audio_converter, metadata_manager,
                 archive: Optional[CoverArchive], write_library_tags: bool, mode: str = MODE_APPEND,
                 orientation: str = ORIENTATION_PORTRAIT,
                 progress: Optional[Callable[[int, int, str], None]] = None,
                 tracks: Optional[List[str]] = None) -> ImportResult:
    """Kompletter Import in einem Rutsch (synchron) - fuer Tests und einfache
    Aufrufer. Die GUI laesst den TrackAddWorker als Thread laufen und ruft
    danach finish_import. tracks = nur diese Dateien des Albums (Teilimport)."""
    from gui.workers import TrackAddWorker
    result = ImportResult()
    if mode == MODE_REPLACE:
        clear_folder_tracks(sd_card, folder)
    worker = TrackAddWorker(folder, list(tracks) if tracks else list(album.tracks), audio_converter,
                            metadata_manager)

    def on_added(track):
        folder.tracks.append(track)
        result.added += 1

    worker.track_added.connect(on_added)
    worker.error.connect(lambda name, message: result.errors.append(f"{name}: {message}"))
    if progress is not None:
        worker.progress.connect(progress)
    worker.run()  # synchron im aufrufenden Thread
    folder.tracks.sort(key=lambda t: t.index)
    return finish_import(sd_card, folder, album, archive, metadata_manager, write_library_tags, orientation, result)


def group_tracks_by_album(paths: List[str]) -> List[tuple]:
    """Gezogene Dateien nach Albumordner buendeln: [(Albumpfad, [Dateien])] in
    Reihenfolge des ersten Auftretens"""
    import os
    groups: List[tuple] = []
    for path in paths:
        album_dir = os.path.dirname(os.path.normpath(path))
        for existing, files in groups:
            if existing == album_dir:
                files.append(path)
                break
        else:
            groups.append((album_dir, [path]))
    return groups
