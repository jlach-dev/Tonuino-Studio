"""
Hauptfenster: Modus TonUINO | ESPuino (Teil von MainWindow)

Im ESPuino-Modus heisst die App "ESPuino-Studio". SD-Karte, Ordnerliste,
RFID-Panel, Systemdateien und SD leeren sind ausgeblendet; statt der SD-Ansicht
zeigt der erste Reiter die Karten - direkt vom ESPuino im WLAN
(core/espuino_device.py) oder aus dem ESPuino-Mediahub (core/mediahub_cards.py) -
mit Cover-Manager, KI-Stapel und Druckbogen. Direkt am ESPuino lassen sich
Karten auch zuweisen. Die Bibliothek bleibt.
Der Modus wird beim Start angewendet; ein Wechsel wirkt nach einem Neustart.

Eigene Datei des Forks, damit gui/main_window.py nah am Original bleibt. Die
Methoden laufen als Teil von MainWindow und greifen auf dessen Attribute zu;
der Mixin steht in der Klassenliste vorn, damit seine Ueberschreibungen gelten.
"""

import logging

from PyQt6.QtGui import QAction, QActionGroup
from PyQt6.QtWidgets import QMenu, QMessageBox

from core import espuino_device as dev
from core import mediahub_cards as mh
from core.app_mode import MODE_ESPUINO, MODE_TONUINO, MODES, current_mode, display_name, set_mode
from core.card_store import card_paths
from gui.about_dialog import show_about
from gui.cover_manager_dialog import CoverManagerDialog, get_archive
from gui.espuino_dialogs import AssignCardDialog, EspuinoSetupDialog
from gui.mediahub_view import MediahubPage, MediahubSidebar
from gui.print_sheet_dialog import PrintSheetDialog

log = logging.getLogger(__name__)

MODE_MENU_LABELS = {
    MODE_TONUINO: "TonUINO – SD-Karte und RFID-Karten",
    MODE_ESPUINO: "ESPuino – Karten vom Gerät oder aus dem Mediahub",
}


class MediahubMixin:
    """Methoden von MainWindow (siehe Modul-Doku)"""

    def _is_espuino(self) -> bool:
        return getattr(self, "app_mode", MODE_TONUINO) == MODE_ESPUINO

    # --- Modus anwenden -------------------------------------------------------------

    def _apply_app_mode(self):
        """Nach _setup_ui: Modus-Menue anlegen und im ESPuino-Modus umbauen"""
        self.app_mode = current_mode()
        self._mediahub_data = None
        self._mediahub_media_root = ""
        self._espuino_source = dev.SOURCE_DEVICE
        self._espuino_host = ""
        self._add_mode_menu()
        if not self._is_espuino():
            return
        name = display_name(self.app_mode)
        self.setWindowTitle(name)
        self.sidebar_title.setText(name)
        self.welcome_label.setText(f"Willkommen bei {name}!")
        self.action_about.setText(f"Über {name} …")
        self.action_about.triggered.disconnect()
        self.action_about.triggered.connect(lambda: show_about(self, name))

        for widget in (self.btn_open_sd, self.btn_new_folder, self.btn_purge_card, self.folder_list_label,
                       self.folder_list, self.rfid_panel):
            widget.hide()
        for action in (self.action_show_all_mp3, self.reader_menu.menuAction(), self.action_system_files,
                       self.action_wipe_card, self.action_series_cover_batch, self.action_tag_lookup):
            action.setVisible(False)
        self.library_view.sd_transfer = False  # kein "Auf SD-Karte übertragen" im Kachelmenue
        self._rewire(self.action_card_cover, "Cover-Manager für diese Karte …", self._mediahub_open_cover)
        self._rewire(self.action_card_cover_batch, "Kartencover für alle Karten ohne Cover …",
                     self._mediahub_generate_missing)
        self._rewire(self.action_print_sheet, "Druckbogen (PDF) erstellen …", self._mediahub_print_sheet)
        self.action_mediahub_setup = QAction("ESPuino einrichten …", self)
        self.action_mediahub_setup.triggered.connect(self._mediahub_setup)
        self.settings_menu.insertAction(self.action_system_files, self.action_mediahub_setup)

        self.mediahub_sidebar = MediahubSidebar()
        self.mediahub_sidebar.setup_requested.connect(self._mediahub_setup)
        self.mediahub_sidebar.reload_requested.connect(self._mediahub_reload)
        self.mediahub_sidebar.assign_requested.connect(self._espuino_assign)
        self.mediahub_sidebar.card_selected.connect(self._mediahub_on_card_clicked)
        sidebar_layout = self.folder_list.parentWidget().layout()
        sidebar_layout.insertWidget(sidebar_layout.indexOf(self.folder_list), self.mediahub_sidebar, 1)

        self.mediahub_page = MediahubPage()
        self.mediahub_page.setup_requested.connect(self._mediahub_setup)
        self.mediahub_page.cover_requested.connect(self._mediahub_open_cover)
        self.mediahub_page.batch_requested.connect(self._mediahub_generate_missing)
        self.mediahub_page.print_requested.connect(self._mediahub_print_sheet)
        self.stack.addWidget(self.mediahub_page)
        self._sd_page = self.mediahub_page
        self.view_switch.setTabToolTip(0, "Karten des ESPuino: Kartencover gestalten und drucken")
        self.view_switch.setTabToolTip(1, "Hörspiele und Musik auf PC und NAS – Album-Cover und Tags")
        if self.view_switch.currentIndex() == 0:
            self.stack.setCurrentWidget(self.mediahub_page)
        self._mediahub_reload()

    @staticmethod
    def _rewire(action: QAction, text: str, slot):
        action.setText(text)
        action.triggered.disconnect()
        action.triggered.connect(lambda _checked=False: slot())

    def _add_mode_menu(self):
        menu = QMenu("Modus", self)
        menu.setToolTip("TonUINO (SD-Karte) oder ESPuino (Mediahub) – wirkt nach einem Neustart")
        group = QActionGroup(self)
        group.setExclusive(True)
        for mode in MODES:
            action = menu.addAction(MODE_MENU_LABELS[mode])
            action.setCheckable(True)
            action.setChecked(mode == self.app_mode)
            action.setData(mode)
            action.triggered.connect(lambda _checked, m=mode: self._on_mode_selected(m))
            group.addAction(action)
        self.mode_menu = menu
        self.settings_menu.insertMenu(self.reader_menu.menuAction(), menu)

    def _on_mode_selected(self, mode: str):
        if mode == current_mode():
            return
        set_mode(mode)
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Question)
        box.setWindowTitle("Modus ändern")
        box.setText(f"Der Modus „{MODE_MENU_LABELS[mode]}“ wird nach einem Neustart aktiv. "
                    f"Die App heißt dann {display_name(mode)}.")
        restart = box.addButton("Jetzt neu starten", QMessageBox.ButtonRole.AcceptRole)
        box.addButton("Später", QMessageBox.ButtonRole.RejectRole)
        box.exec()
        if box.clickedButton() is restart:
            self._restart_application()

    # --- Ansicht --------------------------------------------------------------------

    def _on_view_switched(self, index: int):
        if self._is_espuino() and index == 0:
            self.stack.setCurrentWidget(self.mediahub_page)
            return
        super()._on_view_switched(index)

    def _show_welcome(self):
        if self._is_espuino():
            self._sd_page = self.mediahub_page
            if self.view_switch.currentIndex() == 0:
                self.stack.setCurrentWidget(self.mediahub_page)
            return
        super()._show_welcome()

    def _status(self, text: str):
        status_bar = getattr(self, "status_bar", None)  # beim Start noch nicht angelegt
        if status_bar is not None:
            status_bar.showMessage(text)

    # --- Karten lesen ---------------------------------------------------------------

    def _mediahub_setup(self):
        source, host, sd_root = dev.settings()
        db_path, media_root = mh.settings_paths()
        dialog = EspuinoSetupDialog(source, host, sd_root, db_path, media_root, self)
        if dialog.exec():
            if dialog.source == dev.SOURCE_DEVICE:
                dev.save_settings(dev.SOURCE_DEVICE, dialog.host, dialog.sd_root)
            else:
                dev.save_settings(dev.SOURCE_MEDIAHUB, host, sd_root)
                mh.save_settings_paths(dialog.db_path, dialog.media_root)
            self._mediahub_reload()

    def _espuino_is_device(self) -> bool:
        return self._espuino_source == dev.SOURCE_DEVICE

    def _espuino_place(self) -> str:
        return "auf der SD-Karte" if self._espuino_is_device() else "in der Mediathek"

    def _mediahub_reload(self):
        source, host, sd_root = dev.settings()
        self._espuino_source, self._espuino_host = source, host
        sidebar, page = self.mediahub_sidebar, self.mediahub_page
        device = source == dev.SOURCE_DEVICE
        self.view_switch.setTabText(0, "ESPuino-Karten" if device else "Mediahub-Karten")
        page.overview_title.setText("ESPuino-Karten" if device else "Mediahub-Karten")
        if device:
            configured, root = bool(host), sd_root
            sidebar.btn_assign.setVisible(True)
            sidebar.btn_assign.setEnabled(configured)
            not_configured = ("Noch nicht eingerichtet: die Adresse des ESPuino im WLAN und seine SD-Karte "
                              "im Kartenleser wählen. Studio liest dann seine Karten und kann neue zuweisen.")
        else:
            db_path, root = mh.settings_paths()
            configured = bool(db_path)
            sidebar.btn_assign.setVisible(False)
            not_configured = ("Noch nicht eingerichtet: die db.json des ESPuino-Mediahub und die Mediathek "
                              "wählen. Studio liest die Karten nur, am Mediahub ändert sich nichts.")
        sidebar.btn_reload.setEnabled(configured)
        self._mediahub_media_root = root
        if not configured:
            self._mediahub_data = None
            sidebar.set_cards([], {})
            page.show_overview(not_configured, configured=False)
            return
        try:
            data = dev.load_cards(host, root) if device else mh.load_cards(db_path)
        except (mh.MediahubError, dev.DeviceError) as exc:
            log.warning("ESPuino-Karten: %s", exc)
            self._mediahub_data = None
            sidebar.set_cards([], {})
            page.show_overview(str(exc), configured=False)
            self._status("Karten konnten nicht gelesen werden")
            return
        self._mediahub_data = data
        has_cover = self._mediahub_cover_map()
        selected = sidebar.current_card_id()
        sidebar.set_cards(data.cards, has_cover)
        if selected and mh.card_by_id(data, selected) is not None:
            sidebar.card_list.blockSignals(True)
            sidebar.select(selected)
            sidebar.card_list.blockSignals(False)
            self._mediahub_show_card(selected)
        else:
            self._mediahub_show_overview(has_cover)
        self._status(f"{len(data.cards)} Karten " + ("vom ESPuino" if device else "aus dem Mediahub") + " eingelesen")

    def _espuino_assign(self, preset_card: str = ""):
        if not self._espuino_is_device() or not self._espuino_host:
            return
        _source, host, sd_root = dev.settings()
        known = {c.card_id: c.label for c in (self._mediahub_data.cards if self._mediahub_data else [])}
        dialog = AssignCardDialog(host, sd_root, known, self)
        if preset_card:
            dialog.card_edit.setText(preset_card)
        accepted = dialog.exec()
        entry = dialog.result_entry
        dialog.deleteLater()
        if not accepted or not entry:
            return
        card_id = str(entry.get("id"))
        self._status(f"Karte {mh.format_card_id(card_id)} zugewiesen: {entry.get('fileOrUrl')}")
        self._mediahub_reload()
        if mh.card_by_id(self._mediahub_data, card_id) is not None:
            self.mediahub_sidebar.select(card_id)

    def _mediahub_cover_map(self) -> dict:
        archive = get_archive(self, ask=False)
        result = {}
        for card in self._mediahub_data.cards if self._mediahub_data else []:
            result[card.card_id] = archive is not None and card_paths(str(archive.card_dir(card.album_id)))[0].is_file()
        return result

    def _mediahub_show_overview(self, has_cover: dict):
        data = self._mediahub_data
        total = len(data.cards)
        without = sum(1 for c in data.cards if not has_cover.get(c.card_id))
        origin = "vom ESPuino" if self._espuino_is_device() else "aus dem Mediahub"
        lines = [f"{total} Karte{'' if total == 1 else 'n'} {origin}, davon {without} ohne Kartencover."]
        skipped = []
        if data.skipped_webradio:
            skipped.append(f"{data.skipped_webradio} Webradio-Karte{'' if data.skipped_webradio == 1 else 'n'}")
        if data.skipped_empty:
            skipped.append(f"{data.skipped_empty} ohne Dateien")
        if data.pending:
            skipped.append(f"{data.pending} noch nicht zugewiesen")
        if data.skipped_function:
            skipped.append(f"{data.skipped_function} Funktionskarte{'' if data.skipped_function == 1 else 'n'}")
        if data.skipped_mediahub:
            skipped.append(f"{data.skipped_mediahub} vom Mediahub verwaltet")
        if skipped:
            lines.append("Nicht gezeigt: " + ", ".join(skipped) + ".")
        if not self._mediahub_media_root:
            lines.append(("SD-Karte" if self._espuino_is_device() else "Mediathek")
                         + " fehlt – für Cover bitte unter „ESPuino einrichten“ wählen.")
        lines.append("\nLinks eine Karte wählen.")
        self.mediahub_page.show_overview("\n".join(lines), configured=True, can_batch=without > 0,
                                         can_print=total > without)

    def _mediahub_card(self, card_id: str = ""):
        card_id = card_id or self.mediahub_sidebar.current_card_id()
        return mh.card_by_id(self._mediahub_data, card_id)

    def _mediahub_show_card(self, card_id: str):
        card = self._mediahub_card(card_id)
        if card is None:
            return
        folder = mh.build_folder(card, self._mediahub_media_root, get_archive(self, ask=False))
        self.mediahub_page.show_card(card, folder, self._espuino_place())

    def _mediahub_on_card_clicked(self, card_id: str):
        self._mediahub_show_card(card_id)
        if self.view_switch.currentIndex() != 0:
            self.view_switch.setCurrentIndex(0)

    def _mediahub_folders(self, archive) -> list:
        data = self._mediahub_data
        return [mh.build_folder(c, self._mediahub_media_root, archive) for c in (data.cards if data else [])]

    def _mediahub_require_data(self) -> bool:
        if self._mediahub_data is None:
            QMessageBox.information(self, "ESPuino", "Bitte zuerst einrichten "
                                                     "(Einstellungen → ESPuino einrichten).")
            return False
        return True

    # --- Kartencover ----------------------------------------------------------------

    def _mediahub_open_cover(self, card_id: str = ""):
        if not self._mediahub_require_data():
            return
        card = self._mediahub_card(card_id)
        if card is None:
            QMessageBox.information(self, "Keine Karte gewählt", "Bitte links eine Karte auswählen.")
            return
        archive = get_archive(self)
        if archive is None:
            return
        folder = mh.build_folder(card, self._mediahub_media_root, archive)
        if not folder.tracks:
            QMessageBox.information(self, "Keine Dateien", f"Keine Datei dieser Karte {self._espuino_place()} "
                                                           "gefunden. Bitte unter „ESPuino einrichten“ prüfen.")
            return
        self._release_files()
        dialog = CoverManagerDialog(folder, card.label, self.metadata_manager, archive, self,
                                    focus=CoverManagerDialog.FOCUS_CARD, album_id=folder.album_id, mediahub=True)
        dialog.exec()
        changed = dialog.card_changed
        dialog.deleteLater()  # sonst bleibt jeder Dialog samt Bildern bis zum Programmende im Speicher
        if changed:
            self._status(f"Kartencover für „{card.label}“ gespeichert")
        self.mediahub_sidebar.set_has_cover(card, self._mediahub_cover_map().get(card.card_id, False))
        if self.mediahub_page.card is not None and self.mediahub_page.card.card_id == card.card_id:
            self._mediahub_show_card(card.card_id)

    def _mediahub_generate_missing(self):
        if not self._mediahub_require_data():
            return
        if self._cover_batch_worker is not None and self._cover_batch_worker.isRunning():
            return
        archive = get_archive(self)
        if archive is None:
            return
        folders = [f for f in self._mediahub_folders(archive) if f.tracks and not f.card_path]
        if not folders:
            QMessageBox.information(self, "Nichts zu tun", f"Alle Karten mit Dateien {self._espuino_place()} "
                                                           "haben bereits ein Kartencover.")
            return
        self._confirm_and_start_cover_batch(folders, "Karten", archive)

    def _on_card_cover_batch_folder_done(self, done):
        if isinstance(done, mh.MediahubFolder):
            done.card_path = str(card_paths(done.card_dir)[0])
            card = mh.card_by_id(self._mediahub_data, done.card_id)
            if card is not None:
                self.mediahub_sidebar.set_has_cover(card, True)
            if self.mediahub_page.card is not None and self.mediahub_page.card.card_id == done.card_id:
                self.mediahub_page.set_preview(done.card_path)
            return
        super()._on_card_cover_batch_folder_done(done)

    def _on_card_cover_batch_finished(self, done: int):
        super()._on_card_cover_batch_finished(done)
        if self._is_espuino() and self._mediahub_data is not None:
            self._mediahub_reload()

    def _mediahub_print_sheet(self):
        if not self._mediahub_require_data():
            return
        folders = [f for f in self._mediahub_folders(get_archive(self, ask=False)) if f.card_path]
        if not folders:
            QMessageBox.information(self, "Keine Kartencover", "Es gibt noch keine gespeicherten Kartencover "
                                                               "für die ESPuino-Karten.")
            return
        PrintSheetDialog(folders, {}, self).exec()
