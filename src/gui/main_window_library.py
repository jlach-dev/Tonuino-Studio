"""
Hauptfenster: Bibliothek und Import - Ansicht umschalten, Einlesen, Drag & Drop
auf die SD-Karte, Import-Warteschlange, Stuecke erkennen, Cover nachtragen,
Einsortieren (Teil von MainWindow)

Eigene Datei des Forks, damit gui/main_window.py nah am Original bleibt und
beim Nachziehen weniger Konflikte entstehen. Die Methoden laufen als Teil von
MainWindow und greifen auf dessen Attribute zu.
"""

import logging
import os
from typing import Optional

from PyQt6.QtCore import Qt, QSettings
from PyQt6.QtWidgets import QMessageBox, QApplication, QProgressDialog, QDialog

from core import library_import
from core.ai_settings import SETTINGS_ORIENTATION
from core.card_layout import ORIENTATION_PORTRAIT
from core.library import SETTINGS_WRITE_ID_TAGS, Library, LibraryIndex
from core.sd_card import Folder, Track, MAX_TRACKS_PER_FOLDER
from gui.card_cover_dialog import AiSettingsDialog
from gui.common import confirm_action
from gui.cover_manager_dialog import CoverManagerDialog, SETTINGS_DATA_DIR, get_archive
from gui.data_folder_dialog import DataFolderDialog
from gui.library_settings_dialog import LibrarySourcesDialog, library_sources
from gui.tag_diff_dialog import TagDiffDialog
from gui.workers import CoverProposalWorker, RecognizeWorker, TrackAddWorker, LibraryScanWorker, retire_worker

log = logging.getLogger(__name__)


class LibraryMixin:
    """Methoden von MainWindow (siehe Modul-Doku)"""

    def _file_job_running(self, include_import: bool = True) -> bool:
        """Laeuft noch ein Kopier-, Erkennungs- oder Cover-Vorgang? Dann keinen
        zweiten starten: zwei Kopier-Worker koennten dieselbe Tracknummer belegen.
        include_import=False erlaubt weitere Alben fuer die Import-Warteschlange
        (die arbeitet sie nacheinander ab)."""
        jobs = [getattr(self, "_recognize_job", None), getattr(self, "_embed_job", None)]
        if include_import:
            jobs.append(self._import_job)
        workers = [self._add_tracks_worker] + [job["worker"] for job in jobs if job]
        if any(worker is not None and worker.isRunning() for worker in workers):
            QMessageBox.information(self, "Bitte warten",
                                    "Es läuft noch ein Kopier- oder Suchvorgang. Bitte warten, bis er fertig ist.")
            return True
        return False

    def _show_welcome(self):
        """Willkommensseite als SD-Seite merken; nur anzeigen, wenn die SD-Ansicht aktiv ist"""
        self._sd_page = self.welcome_widget
        if self.view_switch.currentIndex() == 0:
            self.stack.setCurrentWidget(self.welcome_widget)

    def _on_view_switched(self, index: int):
        if index == 1:
            self.stack.setCurrentWidget(self.library_view)
            if not self._library_scanned:
                self._start_library_scan()
        else:
            self.stack.setCurrentWidget(self._sd_page)

    def _set_drop_enabled(self, enabled: bool):
        self.folder_list.drop_enabled = enabled
        self.folder_widget.drop_enabled = enabled
        self.library_view.set_sd_available(enabled)  # Kontextmenue "Auf SD-Karte uebertragen"

    def _init_library(self):
        """Index aus dem Datenordner laden (ohne Netz-/NAS-Zugriff), damit die
        Bibliothek sofort steht; eingelesen wird beim ersten Wechsel in die Ansicht"""
        data_dir = QSettings().value(SETTINGS_DATA_DIR, "", type=str)
        if not data_dir:
            self._library = None
            self.library_view.set_library(None, LibraryIndex())
            return
        self._library = Library(data_dir, library_sources())
        self._library_index = self._library.load()
        self.library_view.set_library(self._library, self._library_index, get_archive(self, ask=False))

    def _edit_library_sources(self):
        if self._library is None and get_archive(self) is None:
            return  # Datenordner wird dafuer gebraucht (Index, Vorschaubilder)
        dialog = LibrarySourcesDialog(self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self._init_library()
            self._start_library_scan(force=True)

    def _change_data_folder(self):
        """Der Index und alle Cover haengen am Ordner, also nicht waehrend eines laufenden Vorgangs"""
        if self._file_job_running():
            return
        running = [self._library_worker, self._cover_batch_worker]
        if any(worker is not None and worker.isRunning() for worker in running):
            QMessageBox.information(self, "Bitte warten", "Die Bibliothek wird gerade eingelesen oder es werden "
                                                          "Kartencover erzeugt. Bitte warten, bis das fertig ist.")
            return
        dialog = DataFolderDialog(self)
        dialog.exec()
        if dialog.changed:
            self._init_library()
            if self._library_scanned:
                self._start_library_scan()
            self.status_bar.showMessage(f"Datenordner: {dialog.current}")

    def _stop_library_worker(self):
        worker = self._library_worker
        if worker is not None:
            worker.cancel()
            worker.wait(5000)
            self._library_worker = None

    def _sd_folders(self) -> list:
        return [f for _, f in sorted(self.sd_card.folders.items())] if self.sd_card else []

    def _start_library_scan(self, force: bool = False):
        """Bibliothek im Hintergrund einlesen (inkrementell) und mit dem Archiv abgleichen"""
        if self._library is None:
            if force:
                self._init_library()
            if self._library is None:
                if force:
                    self._edit_library_sources()
                return
        if self._library_worker is not None and self._library_worker.isRunning():
            self._library_rescan_pending = True  # nach dem laufenden Abgleich nachholen
            return
        self._library_rescan_pending = False
        self._library.sources = library_sources()
        self._library_scanned = True
        self.library_view.set_scanning(True)
        self.action_library_rescan.setEnabled(False)
        worker = LibraryScanWorker(self._library, self._library_index, get_archive(self, ask=False),
                                   self._sd_folders(), rescan=True)
        worker.progress.connect(self._on_library_scan_progress)
        worker.completed.connect(self._on_library_scan_completed)
        worker.failed.connect(self._on_library_scan_failed)
        self._library_worker = worker
        worker.start()

    def _refresh_sd_album_map(self):
        """Nur den Abgleich SD-Karte <-> Bibliothek erneuern (kein Einlesen der Quellen)"""
        if self._library is None or not self.sd_card:
            return
        if self._library_worker is not None and self._library_worker.isRunning():
            return
        worker = LibraryScanWorker(self._library, self._library_index, None, self._sd_folders(), rescan=False)
        worker.completed.connect(self._on_library_scan_completed)
        worker.failed.connect(lambda _msg: None)
        self._library_worker = worker
        worker.start()

    def _on_library_scan_progress(self, current: int, total: int, name: str):
        self.library_view.hint_label.setText(f"Bibliothek wird eingelesen … ({current}/{total}) {name}")

    def _on_library_scan_completed(self, index, sd_map: dict):
        self._library_index = index
        self._sd_album_map = dict(sd_map or {})
        self.library_view.set_scanning(False)
        self.action_library_rescan.setEnabled(True)
        self.library_view.set_library(self._library, index, get_archive(self, ask=False))
        self.library_view.set_sd_map(self._sd_album_map)
        if self.current_folder is not None:
            self._update_library_hint(self.current_folder)
        if self._library_rescan_pending:
            self._start_library_scan()

    def _on_library_scan_failed(self, message: str):
        self.library_view.set_scanning(False)
        self.action_library_rescan.setEnabled(True)
        self.library_view.hint_label.setText(f"Bibliothek konnte nicht eingelesen werden: {message}")

    def _folder_album_id(self, folder: Folder) -> str:
        """Album-ID eines SD-Ordners: card.json, sonst der ID-Tag der ersten Tracks"""
        from core import album_identity, card_store
        if folder.is_special:
            return ""
        meta = card_store.load_meta(folder.path)
        if meta is not None and meta.album_id:
            return meta.album_id
        return album_identity.read_album_id([t.filepath for t in folder.tracks]) or ""

    def _update_library_hint(self, folder: Folder):
        """'aus Bibliothek: <Album>' im Ordnerkopf, plus Hinweis, wenn die
        Bibliothek mehr Tracks hat als der SD-Ordner"""
        album = None
        if self._library is not None and not folder.is_special and folder.tracks:
            album_id = self._folder_album_id(folder)
            album = self._library_index.by_id().get(album_id) if album_id else None
        if album is None:
            self.folder_library_hint.hide()
            return
        text = f"aus Bibliothek: {album.name}"
        warning = album.track_count > len(folder.tracks)
        if warning:
            text += f" · Bibliothek hat {album.track_count - len(folder.tracks)} Track(s) mehr"
        self.folder_library_hint.setText(text)
        self.folder_library_hint.setToolTip(album.path)
        self.folder_library_hint.setProperty("warning", warning)
        self.folder_library_hint.style().unpolish(self.folder_library_hint)
        self.folder_library_hint.style().polish(self.folder_library_hint)
        self.folder_library_hint.show()

    def _on_albums_dropped_on_current(self, paths: list):
        folder = self.current_folder
        identity = folder.index if folder is not None and not folder.is_special else None
        self._on_albums_dropped(paths, identity)

    def _on_albums_dropped(self, paths: list, identity):
        """Alben (Pfade) auf die Ordnerliste bzw. einen Ordner gezogen. Ohne Ziel
        bekommt jedes Album einen neuen Ordner; auf einem Ordner mit Tracks wird
        gefragt: Ersetzen / Anhaengen / Abbrechen."""
        if not self.sd_card or not paths:
            return
        if self._file_job_running(include_import=False):
            return
        if isinstance(identity, str):
            QMessageBox.information(self, "Systemordner", "In die Tonuino-Systemordner (mp3/advert) können "
                                                          "keine Alben gelegt werden.")
            return
        target = self.sd_card.folders.get(identity) if isinstance(identity, int) else None
        mode = library_import.MODE_APPEND
        if target is not None and target.tracks:
            mode = self._ask_replace_or_append(target, len(paths))
            if mode is None:
                return
        for i, path in enumerate(paths):
            # mehrere Alben auf einen Ordner: nur das erste ersetzt, die weiteren haengen an
            self._import_queue.append((path, target, mode if i == 0 else library_import.MODE_APPEND, None))
        self._start_next_import()

    def _on_tracks_dropped(self, paths: list, identity):
        """Einzelne Tracks (aus dem Album-Panel oder dem Explorer) auf die Ordnerliste
        bzw. einen Ordner gezogen: je Album ein Import nur dieser Dateien (haengt an)."""
        if not self.sd_card or not paths:
            return
        if self._file_job_running(include_import=False):
            return
        if isinstance(identity, str):
            QMessageBox.information(self, "Systemordner", "In die Tonuino-Systemordner (mp3/advert) können "
                                                          "keine Tracks gelegt werden.")
            return
        target = self.sd_card.folders.get(identity) if isinstance(identity, int) else None
        for album_dir, files in library_import.group_tracks_by_album(paths):
            self._import_queue.append((album_dir, target, library_import.MODE_APPEND, files))
        self._start_next_import()

    def _library_tags(self, album, paths: Optional[list] = None):
        """Tags eines Bibliotheksalbums (oder nur der gewaehlten Dateien) aus dem Internet nachtragen"""
        if album is None:
            return
        files = list(paths) if paths else list(album.tracks)
        if not files:
            return
        self.player_bar.stop()
        dialog = TagDiffDialog(files, self.metadata_manager, self, album.title or album.name, album.artist,
                               label=album.name)
        if dialog.exec() == QDialog.DialogCode.Accepted and dialog.written:
            self.status_bar.showMessage(f"Tags in {dialog.written} Datei(en) von „{album.name}“ geschrieben")
            self._refresh_library_album(album.path)

    def _refresh_library_album(self, path: str):
        """Nach Tag-Aenderungen: Album neu einlesen, Index speichern, Ansicht auffrischen"""
        if self._library is None:
            return
        try:
            self._library_index = self._library.refresh_album(path, self._library_index,
                                                              get_archive(self, ask=False))
        except Exception:
            log.exception("Album konnte nicht neu eingelesen werden")
            return
        self.library_view.set_library(self._library, self._library_index, get_archive(self, ask=False))
        if self.library_view.open_album_path == path:
            self.library_view.open_album(path)

    def _library_recognize(self, album, paths):
        """Stuecke eines Bibliotheksalbums (oder nur der markierten) per AcoustID erkennen"""
        from core import ai_settings
        from core.acoustid import find_fpcalc
        if album is None or self._file_job_running():
            return
        files = list(paths) if paths else list(album.tracks)
        if not files:
            return
        key = ai_settings.get_acoustid_key()
        fpcalc = find_fpcalc(QSettings().value(ai_settings.SETTINGS_FPCALC, "", type=str),
                             QSettings().value("library/data_dir", "", type=str))
        if not key or not fpcalc:
            missing = " und ".join(p for p, ok in (("ein AcoustID-Key", key), ("fpcalc", fpcalc)) if not ok)
            if confirm_action(self, "Stücke erkennen", f"Zum Erkennen fehlt noch {missing}.\n\n"
                              "Jetzt die Einstellungen (KI & Online-Dienste) öffnen?", default_no=False):
                AiSettingsDialog(self).exec()
            return
        dialog = QProgressDialog("Stücke werden erkannt (Fingerabdruck + AcoustID) …", "Abbrechen", 0, len(files),
                                 self)
        dialog.setWindowTitle("Stücke erkennen")
        dialog.setWindowModality(Qt.WindowModality.WindowModal)
        dialog.setMinimumDuration(0)
        dialog.setValue(0)
        worker = RecognizeWorker(files, key, fpcalc)
        self._recognize_job = {"worker": worker, "dialog": dialog, "album": album, "files": files}
        worker.progress.connect(lambda cur, total, name: self._on_import_progress(dialog, cur, total, name))
        worker.done.connect(self._on_recognized)
        worker.failed.connect(lambda m: (dialog.close(), QMessageBox.warning(self, "Erkennung fehlgeschlagen", m)))
        dialog.canceled.connect(worker.cancel)
        worker.start()

    def _library_cover(self, album):
        """Cover-Manager fuer ein Bibliotheksalbum: Album-Cover in die Dateien (+ cover.jpg),
        Kartencover ins Archiv"""
        if album is None or not album.tracks:
            return
        archive = get_archive(self)
        if archive is None:
            return
        self.player_bar.stop()
        self._release_files()
        tracks = [Track(index=i, filename=os.path.basename(p), filepath=p)
                  for i, p in enumerate(album.tracks, start=1)]
        cover_file = next((os.path.join(album.path, n) for n in ("cover.jpg", "cover.png", "folder.jpg")
                           if os.path.isfile(os.path.join(album.path, n))), "")
        folder = Folder(index=None, path=album.path, tracks=tracks, cover_path=cover_file, name=album.name)
        dialog = CoverManagerDialog(folder, album.title or album.name, self.metadata_manager, archive, self,
                                    focus=CoverManagerDialog.FOCUS_ALBUM, library=True, album_id=album.album_id)
        dialog.exec()
        if dialog.tags_changed or dialog.album_cover_changed or dialog.card_changed:
            if dialog.album_cover_changed:
                self.status_bar.showMessage(f"Album-Cover von „{album.name}“ in die Bibliothek geschrieben")
            self._refresh_library_album(album.path)
        dialog.deleteLater()  # siehe _open_card_cover_dialog

    def _library_embed_covers(self):
        """Cover-Stapelsuche: Vorschlaege fuer alle Bibliotheksalben ohne Cover (Archiv,
        Release-ID, Online-Suche mit Bewertung), dann Pruefdialog"""
        from core import cover_batch
        from gui.cover_manager_dialog import SETTINGS_SOURCES
        if self._library is None or not self._library_index.albums:
            QMessageBox.information(self, "Cover nachtragen", "Die Bibliothek ist leer.")
            return
        if self._file_job_running():
            return
        archive = get_archive(self)
        if archive is None:
            return
        todo = cover_batch.albums_without_cover(self._library_index)
        if not todo:
            QMessageBox.information(self, "Cover nachtragen", "Alle erreichbaren Alben haben bereits ein Cover.")
            return
        chosen = QSettings().value(SETTINGS_SOURCES, list(cover_batch.SEARCH_SOURCES), type=list)
        sources = [s for s in cover_batch.SEARCH_SOURCES if s in chosen] or list(cover_batch.SEARCH_SOURCES)
        if not confirm_action(self, "Cover nachtragen",
                              f"Für {len(todo)} Album/Alben ohne Cover werden Vorschläge gesucht: aus dem Archiv, "
                              "zur MusicBrainz-Release-ID in den Tags oder per Online-Suche "
                              f"({', '.join(sources)}). Geschrieben wird erst nach deiner Prüfung.\n\nFortfahren?",
                              default_no=False):
            return
        self.player_bar.stop()
        self._release_files()
        dialog = QProgressDialog("Cover-Vorschläge werden gesucht …", "Abbrechen", 0, len(todo), self)
        dialog.setWindowTitle("Cover nachtragen")
        dialog.setWindowModality(Qt.WindowModality.WindowModal)
        dialog.setMinimumDuration(0)
        dialog.setValue(0)
        worker = CoverProposalWorker(self._library_index, archive, sources)
        self._embed_job = {"worker": worker, "dialog": dialog, "archive": archive}
        worker.progress.connect(lambda cur, total, name: self._on_import_progress(dialog, cur, total, name))
        worker.done.connect(lambda proposals: self._on_cover_proposals(dialog, archive, proposals))
        worker.failed.connect(lambda m: (dialog.close(), QMessageBox.warning(self, "Cover nachtragen", m)))
        dialog.canceled.connect(worker.cancel)
        worker.start()

    def _on_cover_proposals(self, progress, archive, proposals):
        from gui.cover_proposal_dialog import CoverProposalDialog
        progress.close()
        job, self._embed_job = self._embed_job, None
        if job:
            retire_worker(job["worker"])  # run() kehrt nach dem Signal erst noch zurueck
        if not proposals:
            QMessageBox.information(self, "Cover nachtragen", "Keine Vorschläge gefunden.")
            return
        dialog = CoverProposalDialog(proposals, archive, self)
        accepted = dialog.exec() == QDialog.DialogCode.Accepted
        if accepted and dialog.result is not None:
            result = dialog.result
            text = f"{result.albums} Album/Alben mit Cover versehen ({result.tracks} Datei(en))"
            if result.skipped:
                text += f", {result.skipped} übersprungen"
            self.status_bar.showMessage(text)
            self._start_library_scan(force=True)
        elif dialog.open_requested is not None:
            self._library_cover(dialog.open_requested)

    def _library_organize(self, album, paths):
        """Dateien eines Bibliotheksalbums nach Tags in Interpret/Album-Ordner verschieben"""
        from gui.organize_dialog import OrganizeDialog
        if album is None:
            return
        files = list(paths) if paths else list(album.tracks)
        root = album.source or (self._library._source_of(album.path) if self._library else "") or \
            os.path.dirname(album.path)
        if not files or not root:
            return
        self.player_bar.stop()
        dialog = OrganizeDialog(files, root, self, label=album.name)
        if dialog.exec() == QDialog.DialogCode.Accepted and dialog.result is not None:
            moved = dialog.result.moved
            self.status_bar.showMessage(f"{moved} Datei(en) einsortiert" + (
                f", {dialog.result.removed_dirs} leere Ordner entfernt" if dialog.result.removed_dirs else ""))
            self.library_view.show_overview()
            self._start_library_scan(force=True)

    def _on_recognized(self, result, candidates):
        from gui.recognition_dialog import RecognitionDialog
        job, self._recognize_job = getattr(self, "_recognize_job", None), None
        if job is None:
            return
        retire_worker(job["worker"])  # run() kehrt nach dem Signal erst noch zurueck
        job["dialog"].close()
        album, files = job["album"], job["files"]
        if result.recognized == 0:
            QMessageBox.information(self, "Stücke erkennen", "Keine der Dateien wurde erkannt. "
                                    + (f"Fehler: {result.files[0].error}" if result.files and result.files[0].error
                                       else "Die Aufnahmen sind AcoustID offenbar unbekannt."))
            return
        written = 0
        if candidates and result.agreeing >= max(1, len(files) // 2):
            # gemeinsames Album erkannt: Tag-Dialog mit den Ausgaben dieser Release-Group
            self.player_bar.stop()
            dialog = TagDiffDialog(files, self.metadata_manager, self, album.title or album.name, album.artist,
                                   label=f"{album.name} (erkannt: {result.release_group_title})",
                                   preset=candidates, recording_ids=result.recording_ids())
            if dialog.exec() == QDialog.DialogCode.Accepted:
                written = dialog.written
        else:
            dialog = RecognitionDialog(result, self, label=album.name)
            if dialog.exec() == QDialog.DialogCode.Accepted:
                written = dialog.written
        if written:
            self.status_bar.showMessage(f"Tags in {written} Datei(en) von „{album.name}“ geschrieben")
            self._refresh_library_album(album.path)

    def _ask_replace_or_append(self, target: Folder, count: int) -> Optional[str]:
        """Ersetzen / Anhaengen / Abbrechen (None) fuer einen Ordner, der schon Tracks hat"""
        name = self._resolve_folder_name(target)
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Question)
        box.setWindowTitle(f"Ordner {target.index:02d}")
        box.setText(f"Der Ordner „{name}“ enthält bereits {target.track_count} Track(s).\n\n"
                    f"Sollen die vorhandenen Tracks durch die {count} gezogene(n) Album/Alben ersetzt werden, "
                    "oder sollen sie angehängt werden?")
        btn_replace = box.addButton("Ersetzen", QMessageBox.ButtonRole.DestructiveRole)
        btn_append = box.addButton("Anhängen", QMessageBox.ButtonRole.AcceptRole)
        box.addButton("Abbrechen", QMessageBox.ButtonRole.RejectRole)
        box.setDefaultButton(btn_append)
        box.exec()
        if box.clickedButton() == btn_replace:
            return library_import.MODE_REPLACE
        if box.clickedButton() == btn_append:
            return library_import.MODE_APPEND
        return None

    def _ask_write_id_tags(self) -> bool:
        """Einmalige Rueckfrage, ob die Album-ID auch in die Bibliotheksdateien darf"""
        settings = QSettings()
        decision = settings.value(SETTINGS_WRITE_ID_TAGS, "", type=str)
        if decision in ("yes", "no"):
            return decision == "yes"
        allowed = confirm_action(
            self, "Album-ID in die Bibliothek schreiben?",
            "Damit ein Album nach Umbenennen oder Verschieben wiedererkannt wird, schreibt der Manager eine "
            "Album-ID als Tag (TXXX:TONUINO_ALBUM_ID) in die Audiodateien.\n\n"
            "Auf die SD-Karte kommt sie immer. Darf sie auch in die Originaldateien deiner Bibliothek "
            "geschrieben werden? (MP3, FLAC, M4A, OGG – WAV wird übersprungen. Die Musik selbst bleibt "
            "unverändert.)\n\nDiese Entscheidung gilt künftig für alle Importe und lässt sich unter "
            "Einstellungen → Bibliothek-Ordner zurücksetzen.", default_no=False)
        settings.setValue(SETTINGS_WRITE_ID_TAGS, "yes" if allowed else "no")
        return allowed

    def _start_next_import(self):
        if self._import_job is not None or not self._import_queue or not self.sd_card:
            return
        path, folder, mode, only_tracks = self._import_queue.pop(0)
        try:
            album = library_import.library_album_for(path, self._library_index)
        except OSError as e:
            QMessageBox.warning(self, "Album nicht lesbar", f"{path}\n\n{e}")
            self._start_next_import()
            return
        if not album.tracks:
            QMessageBox.information(self, "Keine Audiodateien", f"Im Ordner „{path}“ wurden keine Audiodateien "
                                                                 "gefunden.")
            self._start_next_import()
            return
        write_tags = True if album.album_id else self._ask_write_id_tags()
        if folder is None:
            free = next((i for i in range(1, 100) if i not in self.sd_card.folders), None)
            if free is None:
                QMessageBox.warning(self, "Fehler", "Maximale Anzahl von 99 Ordnern erreicht!")
                self._import_queue.clear()
                return
            try:
                folder = self.sd_card.create_folder(free)
            except Exception as e:
                QMessageBox.warning(self, "Fehler", str(e))
                self._import_queue.clear()
                return
            self._populate_folder_list()
        self._release_files()
        # Ersetzen: erst in einen Zwischenordner kopieren, die alten Tracks
        # werden erst nach dem Kopieren ausgetauscht (_on_import_copied)
        staging = None
        if mode == library_import.MODE_REPLACE:
            try:
                staging = library_import.create_staging_folder(folder)
            except OSError as e:
                QMessageBox.warning(self, "Fehler", f"Zwischenordner für das Ersetzen nicht anlegbar: {e}")
                self._start_next_import()
                return
        target = staging or folder
        free_slots = MAX_TRACKS_PER_FOLDER - target.track_count
        files = [f for f in only_tracks if f in album.tracks] or list(only_tracks) if only_tracks else list(album.tracks)
        if len(files) > free_slots:
            QMessageBox.warning(self, "Zu viele Tracks",
                                f"Ein TonUINO-Ordner kann maximal {MAX_TRACKS_PER_FOLDER} Tracks enthalten. "
                                f"Es werden nur die ersten {max(free_slots, 0)} der {len(files)} Tracks kopiert.")
            files = files[:max(free_slots, 0)]

        what = f"{len(files)} Track(s) aus „{album.name}“" if only_tracks else f"„{album.name}“"
        dialog = QProgressDialog(f"{what} wird kopiert …", "Abbrechen", 0, len(files), self)
        dialog.setWindowTitle(f"Album in Ordner {folder.index:02d}")
        dialog.setWindowModality(Qt.WindowModality.WindowModal)
        dialog.setMinimumDuration(0)
        dialog.setValue(0)
        worker = TrackAddWorker(target, files, self.audio_converter, self.metadata_manager)
        job = {"album": album, "folder": folder, "mode": mode, "worker": worker, "dialog": dialog,
               "previous": list(folder.tracks) if staging is None else None,
               "errors": [], "write_tags": write_tags, "staging": staging}
        self._import_job = job
        worker.progress.connect(lambda cur, total, name: self._on_import_progress(dialog, cur, total, name))
        worker.track_added.connect(lambda track: target.tracks.append(track))
        worker.error.connect(lambda name, msg: job["errors"].append(f"{name}: {msg}"))
        worker.completed.connect(self._on_import_copied)
        dialog.canceled.connect(worker.cancel)
        worker.start()

    @staticmethod
    def _on_import_progress(dialog: QProgressDialog, current: int, total: int, name: str):
        dialog.setLabelText(f"({current}/{total}) {name}")
        # Das Signal kommt vor dem Element: mit current haette der Dialog beim Start
        # des letzten Elements sein Maximum erreicht und sich geschlossen, obwohl
        # noch gearbeitet wird - und das Fenster waere wieder bedienbar
        dialog.setValue(current - 1)

    def _on_import_copied(self, added: int):
        job, self._import_job = self._import_job, None
        if job is None:
            return
        job["cancelled"] = job["worker"].is_cancelled  # vorher lesen: retire_worker bricht den Worker selbst ab
        retire_worker(job["worker"])  # run() kehrt nach dem Signal erst noch zurueck
        job["dialog"].close()
        folder, album = job["folder"], job["album"]
        if self.sd_card is None:
            return  # Karte wurde waehrend des Kopierens entfernt
        staging = job["staging"]
        if staging is not None and not self._commit_replace(job, added):
            if self._import_queue:
                self._start_next_import()
            return
        folder.tracks.sort(key=lambda t: t.index)
        result = library_import.ImportResult(added=added, errors=list(job["errors"]))
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            self._release_files()
            orientation = QSettings().value(SETTINGS_ORIENTATION, ORIENTATION_PORTRAIT, type=str)
            result = library_import.finish_import(self.sd_card, folder, album, get_archive(self, ask=False),
                                                  self.metadata_manager, job["write_tags"], orientation, result,
                                                  previous_tracks=job["previous"])
        finally:
            QApplication.restoreOverrideCursor()
        if result.album_id:
            self._sd_album_map[result.album_id] = folder.index
        self._folder_name_cache.pop(folder.index, None)
        name = self._manual_folder_name(folder) or album.title
        if name:
            self._folder_name_cache[folder.index] = name
        self._populate_folder_list()
        self._select_folder_in_list(folder.index)
        self.status_bar.showMessage(f"Ordner {folder.index:02d}: {result.summary}")
        if result.errors:
            QMessageBox.warning(self, "Import mit Fehlern", "\n".join(result.errors))
        if self._import_queue:
            self._start_next_import()
        elif self._library is not None:
            self.library_view.set_library(self._library, self._library_index, get_archive(self, ask=False))
            self.library_view.set_sd_map(self._sd_album_map)

    def _commit_replace(self, job: dict, added: int) -> bool:
        """Ersetzen nach dem Kopieren abschliessen. Abgebrochen oder nichts
        kopiert: Zwischenordner verwerfen, die alten Tracks bleiben. Gibt
        zurueck, ob ersetzt wurde (nur dann folgt der Album-Abgleich)."""
        folder, staging = job["folder"], job["staging"]
        self._release_files()
        if job["cancelled"] or added == 0:
            library_import.discard_staging_folder(staging)
            reason = "abgebrochen" if job["cancelled"] else "keine Tracks kopiert"
            self.status_bar.showMessage(f"Ordner {folder.index:02d}: Ersetzen {reason} – die bisherigen Tracks "
                                        "bleiben erhalten")
            if job["errors"]:
                QMessageBox.warning(self, "Ersetzen fehlgeschlagen",
                                    "Es wurde nichts ersetzt, die bisherigen Tracks bleiben erhalten.\n\n"
                                    + "\n".join(job["errors"]))
            return False
        try:
            library_import.commit_staged_replace(self.sd_card, folder, staging)
        except OSError as e:
            QMessageBox.warning(self, "Ersetzen fehlgeschlagen",
                                f"Die Tracks konnten nicht ausgetauscht werden: {e}\n\nDie neu kopierten Tracks "
                                f"liegen ggf. noch in {staging.path}.")
            self._rescan_after_failed_replace()
            return False
        return True

    def _rescan_after_failed_replace(self):
        """Nach einem halb ausgefuehrten Austausch den Kartenstand neu einlesen"""
        try:
            self.sd_card.scan()
        except OSError:
            log.exception("Neu-Einlesen nach fehlgeschlagenem Ersetzen")
        self._populate_folder_list()
