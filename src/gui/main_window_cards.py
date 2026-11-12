"""
Hauptfenster: Kartencover - Cover-Manager, Tags von MusicBrainz, Druckbogen,
Kartencover fuer alle Ordner bzw. eine Serie (Teil von MainWindow)

Eigene Datei des Forks, damit gui/main_window.py nah am Original bleibt und
beim Nachziehen weniger Konflikte entstehen. Die Methoden laufen als Teil von
MainWindow und greifen auf dessen Attribute zu.
"""

from PyQt6.QtCore import Qt, QSettings
from PyQt6.QtWidgets import QMessageBox, QApplication, QProgressDialog, QInputDialog, QDialog
from core.cover_generator import REFUSAL_BATCH_HINT, REFUSED_SHORT
from gui.common import close_progress_dialog, keep_open_until_done

from core.ai_settings import SETTINGS_ORIENTATION, SETTINGS_STYLE
from core.card_layout import ORIENTATION_PORTRAIT
from core.card_store import card_paths
from core.cover_generator import DEFAULT_STYLE, ESTIMATED_COST_PER_IMAGE, SOURCE_AI
from core.cover_research import gather_folder_info
from core.sd_card import Folder
from gui.card_cover_dialog import gemini_client, image_model, text_model
from gui.common import confirm_action
from gui.cover_manager_dialog import CoverManagerDialog, get_archive
from gui.print_sheet_dialog import PrintSheetDialog
from gui.tag_diff_dialog import TagDiffDialog
from gui.workers import CoverBatchWorker


class CardCoverMixin:
    """Methoden von MainWindow (siehe Modul-Doku)"""

    def _require_sd_card(self) -> bool:
        if self.sd_card is None:
            QMessageBox.information(self, "Keine SD-Karte", "Bitte zuerst eine SD-Karte öffnen.")
            return False
        return True

    def _open_card_cover_dialog(self, focus: str = CoverManagerDialog.FOCUS_CARD):
        folder = self.current_folder
        if folder is None or self._is_special_folder(folder) or not folder.tracks:
            QMessageBox.information(self, "Kein Ordner gewählt",
                                    "Bitte zuerst einen Ordner mit Tracks auswählen.")
            return
        self._release_files()
        archive = get_archive(self)
        dialog = CoverManagerDialog(folder, self._resolve_folder_name(folder), self.metadata_manager, archive, self,
                                    focus=focus)
        dialog.exec()
        if dialog.tags_changed:
            self._after_tags_written(folder, dialog.tags_album, dialog.tags_written)
        elif dialog.album_cover_changed and self.current_folder is folder:
            self._show_folder(folder)
        if dialog.card_changed:
            self.status_bar.showMessage(f"Kartencover für Ordner {folder.index:02d} gespeichert")
        dialog.deleteLater()  # sonst bleibt jeder Dialog samt aller Bilder bis zum Programmende im Speicher

    def _open_tag_lookup_dialog(self):
        """Tags des gewaehlten Ordners aus MusicBrainz (bzw. Deezer/iTunes) uebernehmen"""
        folder = self.current_folder
        if folder is None or self._is_special_folder(folder) or not folder.tracks:
            QMessageBox.information(self, "Kein Ordner gewählt",
                                    "Bitte zuerst einen Ordner mit Tracks auswählen.")
            return
        self._release_files()
        dialog = TagDiffDialog(folder, self.metadata_manager, self)
        if dialog.exec() == QDialog.DialogCode.Accepted and dialog.written:
            self._after_tags_written(folder, dialog.applied_album, dialog.written)

    def _after_tags_written(self, folder: Folder, album: str, written: int):
        """Ordnername (Album-Tag) und Trackliste nach dem Tag-Schreiben auffrischen"""
        name = self._manual_folder_name(folder) or album
        if name:
            self._set_folder_name(folder.index, name)
        else:
            self._folder_name_cache.pop(folder.index, None)
        if self.current_folder is folder:
            self._show_folder(folder)
        self.status_bar.showMessage(f"Tags in {written} Track(s) von Ordner {folder.index:02d} geschrieben")

    def _open_print_sheet_dialog(self):
        if not self._require_sd_card():
            return
        folders = [self.sd_card.folders[i] for i in sorted(self.sd_card.folders)]
        if not any(f.card_path for f in folders):
            QMessageBox.information(self, "Keine Kartencover",
                                    "Es gibt noch keine gespeicherten Kartencover auf dieser SD-Karte.")
            return
        PrintSheetDialog(folders, self._folder_name_cache, self).exec()

    def _generate_missing_card_covers(self):
        if not self._require_sd_card():
            return
        if self._cover_batch_worker is not None and self._cover_batch_worker.isRunning():
            return
        folders = [f for _, f in sorted(self.sd_card.folders.items()) if f.tracks and not f.card_path]
        if not folders:
            QMessageBox.information(self, "Nichts zu tun", "Alle Ordner mit Tracks haben bereits ein Kartencover.")
            return
        self._confirm_and_start_cover_batch(folders, "Ordner", get_archive(self))

    def _confirm_and_start_cover_batch(self, folders: list, unit: str, archive):
        """API-Key pruefen, Stil/Format und Kosten bestaetigen lassen, Stapel starten.
        unit: "Ordner" (SD-Karte) bzw. "Karten" (Mediahub)"""
        client = gemini_client()
        if client is None:
            QMessageBox.information(self, "Kein API-Key",
                                    "Für die automatische Erzeugung bitte zuerst unter "
                                    "Kartencover → KI & Online-Dienste einen Gemini-API-Key hinterlegen.")
            return
        settings = QSettings()
        style = settings.value(SETTINGS_STYLE, DEFAULT_STYLE, type=str)
        orientation = settings.value(SETTINGS_ORIENTATION, ORIENTATION_PORTRAIT, type=str)
        orientation_label = "Hochformat" if orientation == ORIENTATION_PORTRAIT else "Querformat"
        if not confirm_action(
                self, "Kartencover erzeugen",
                f"Für {len(folders)} {unit} wird je ein Kartencover erzeugt "
                f"(Stil „{style}“, {orientation_label}).\n\n"
                f"Geschätzte Kosten: ca. {len(folders) * ESTIMATED_COST_PER_IMAGE:.2f} USD. Fortfahren?",
                default_no=False):
            return
        self._start_cover_batch(folders, client, style, orientation, archive=archive)

    def _series_folders(self, profile, archive) -> list:
        """Ordner ohne Kartencover, die zur Serie gehoeren: ueber die Album-ID im
        Archiv zugeordnet oder mit erkannter Reihe gleichen Namens (Tags)"""
        from core import album_identity
        from core.series import detect_series, normalize_series_name
        key = normalize_series_name(profile.name)
        result = []
        for _, folder in sorted(self.sd_card.folders.items()):
            if not folder.tracks or folder.card_path or self._is_special_folder(folder):
                continue
            album_id = album_identity.read_album_id([t.filepath for t in folder.tracks])
            record = archive.load_album(album_id) if album_id else None
            if record is not None and record.series_id:
                if record.series_id == profile.id:
                    result.append(folder)
                continue
            info = gather_folder_info(folder, self.metadata_manager, max_titles=5)
            name, _mb = detect_series(info, lookup=lambda _rg: [])
            if name and normalize_series_name(name) == key:
                result.append(folder)
        return result

    def _series_batch_ready(self) -> bool:
        """Gibt es etwas, woraus der Serienstapel Folgen sucht? (ESPuino-Modus: die Karten)"""
        return self._require_sd_card()

    def _generate_series_card_covers(self):
        """Alle Folgen einer Serie ohne Kartencover: KI-Cover mit Ankern und Stilbibel"""
        if not self._series_batch_ready():
            return
        if self._cover_batch_worker is not None and self._cover_batch_worker.isRunning():
            return
        archive = get_archive(self)
        if archive is None:
            return
        profiles = archive.series.list_all()
        if not profiles:
            QMessageBox.information(self, "Keine Serien", "Es gibt noch keine Serienprofile – im Cover-Manager "
                                                           "im Tab „Serie“ eine Serie anlegen und Anker-Bilder setzen.")
            return
        names = [p.name for p in profiles]
        name, ok = QInputDialog.getItem(self, "Serie wählen", "Hörspielreihe:", names, 0, False)
        if not ok or name not in names:
            return
        profile = profiles[names.index(name)]
        client = gemini_client()
        if client is None:
            QMessageBox.information(self, "Kein API-Key",
                                    "Für die automatische Erzeugung bitte zuerst unter "
                                    "Kartencover → KI & Online-Dienste einen Gemini-API-Key hinterlegen.")
            return
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            folders = self._series_folders(profile, archive)
            anchors = archive.series.anchor_images(profile, archive)
        finally:
            QApplication.restoreOverrideCursor()
        if not folders:
            QMessageBox.information(self, "Nichts zu tun", f"Alle {self._series_unit()} der Serie „{profile.name}“ "
                                                            "haben bereits ein Kartencover (oder keine gehört dazu).")
            return
        settings = QSettings()
        style = profile.style or settings.value(SETTINGS_STYLE, DEFAULT_STYLE, type=str)
        orientation = settings.value(SETTINGS_ORIENTATION, ORIENTATION_PORTRAIT, type=str)
        orientation_label = "Hochformat" if orientation == ORIENTATION_PORTRAIT else "Querformat"
        bible = "mit Stilbibel" if profile.style_bible.strip() else "ohne Stilbibel"
        if not confirm_action(
                self, "Kartencover für die Serie erzeugen",
                f"Für {len(folders)} {self._series_unit()} der Serie „{profile.name}“ wird je ein Kartencover erzeugt "
                f"(Stil „{style}“, {orientation_label}, {len(anchors)} Anker-Bild(er), {bible}).\n\n"
                f"Geschätzte Kosten: ca. {len(folders) * ESTIMATED_COST_PER_IMAGE:.2f} USD. Fortfahren?",
                default_no=False):
            return
        self._start_cover_batch(folders, client, style, orientation, archive=archive, series_profile=profile,
                                anchor_images=anchors)

    def _series_unit(self) -> str:
        return "Ordner"

    def _start_cover_batch(self, folders: list, client, style: str, orientation: str, **worker_kwargs):
        self._release_files()
        self._cover_batch_errors = []
        self._cover_batch_refused = []
        self._cover_batch_dialog = QProgressDialog("Kartencover werden erzeugt …", "Abbrechen", 0, len(folders), self)
        self._cover_batch_dialog.setWindowTitle("Kartencover")
        self._cover_batch_dialog.setWindowModality(Qt.WindowModality.WindowModal)
        self._cover_batch_dialog.setMinimumDuration(0)
        self._cover_batch_dialog.setValue(0)

        worker = CoverBatchWorker(folders, self.metadata_manager, client, text_model(), image_model(),
                                  SOURCE_AI, style, orientation, **worker_kwargs)
        worker.progress.connect(self._on_card_cover_batch_progress)
        worker.folder_done.connect(self._on_card_cover_batch_folder_done)
        worker.error.connect(lambda name, msg: self._cover_batch_errors.append(f"{name}: {msg}"))
        worker.refused.connect(self._cover_batch_refused.append)
        worker.completed.connect(self._on_card_cover_batch_finished)
        keep_open_until_done(self._cover_batch_dialog, worker.cancel, "Wird nach dem aktuellen Cover abgebrochen …")
        self._cover_batch_worker = worker
        worker.start()

    def _on_card_cover_batch_progress(self, current: int, total: int, name: str):
        self._cover_batch_dialog.setValue(current - 1)
        self._cover_batch_dialog.setLabelText(f"({current}/{total}) {name}")

    def _on_card_cover_batch_folder_done(self, folder_index: int):
        folder = self.sd_card.folders.get(folder_index) if self.sd_card else None
        if folder is not None:
            folder.card_path = str(card_paths(folder.path)[0])

    def _on_card_cover_batch_finished(self, done: int):
        close_progress_dialog(self._cover_batch_dialog)
        self.status_bar.showMessage(f"{done} Kartencover erzeugt")
        if self._cover_batch_errors:
            text = "\n".join(self._cover_batch_errors)
            if REFUSED_SHORT in text:
                text += "\n\n" + REFUSAL_BATCH_HINT
            refused = list(getattr(self, "_cover_batch_refused", []))
            box = QMessageBox(QMessageBox.Icon.Warning, "Einige Kartencover fehlgeschlagen", text, parent=self)
            rework = None
            if refused:
                rework = box.addButton(f"Abgelehnte nacharbeiten ({len(refused)}) …",
                                       QMessageBox.ButtonRole.AcceptRole)
                box.setInformativeText("„Nacharbeiten“ öffnet für jede abgelehnte Karte nacheinander den "
                                       "Cover-Manager.")
            box.addButton("Schließen", QMessageBox.ButtonRole.RejectRole)
            box.exec()
            if rework is not None and box.clickedButton() is rework:
                self._rework_refused(refused)

    def _rework_refused(self, folders: list):
        """Abgelehnte Karten nacheinander im Cover-Manager oeffnen; zwischen zweien
        laesst sich aufhoeren"""
        for i, folder in enumerate(folders, start=1):
            if i > 1:
                name = folder.name or (f"Ordner {folder.index:02d}" if folder.index is not None else "Karte")
                answer = QMessageBox.question(self, "Nacharbeiten", f"Weiter mit „{name}“ ({i}/{len(folders)})?")
                if answer != QMessageBox.StandardButton.Yes:
                    return
            self._open_cover_for_batch_folder(folder)

    def _open_cover_for_batch_folder(self, folder):
        """Cover-Manager fuer einen Ordner der SD-Karte (aus einem Stapel)"""
        if folder is None or not folder.tracks:
            return
        self._release_files()
        archive = get_archive(self)
        dialog = CoverManagerDialog(folder, self._resolve_folder_name(folder), self.metadata_manager, archive, self,
                                    focus=CoverManagerDialog.FOCUS_CARD)
        dialog.exec()
        if dialog.card_changed:
            self.status_bar.showMessage(f"Kartencover für Ordner {folder.index:02d} gespeichert")
        dialog.deleteLater()
