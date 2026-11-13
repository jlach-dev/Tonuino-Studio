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
import os

from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtGui import QAction, QActionGroup
from PyQt6.QtWidgets import QApplication, QCheckBox, QMenu, QMessageBox, QProgressDialog

from core import espuino_cards, loudness
from core import espuino_device as dev
from core.espuino_transfer import plan_album
from core import mediahub_cards as mh
from core.app_mode import MODE_ESPUINO, MODE_TONUINO, MODES, current_mode, display_name, set_mode
from core.card_store import card_paths
from gui.about_dialog import show_about
from gui.cover_manager_dialog import CoverManagerDialog, get_archive
from gui.common import close_progress_dialog, confirm_action, keep_open_until_done
from gui.espuino_dialogs import EspuinoSetupDialog, TransferDialog, TransferWorker
from gui.espuino_device_view import EspuinoDeviceSidebar, album_folder_names
from gui.mediahub_view import MediahubPage, MediahubSidebar
from gui.print_sheet_dialog import PrintSheetDialog
from gui.workers import LoudnessAnalysisWorker, LoudnessApplyWorker

log = logging.getLogger(__name__)

MODE_MENU_LABELS = {
    MODE_TONUINO: "TonUINO – SD-Karte und RFID-Karten",
    MODE_ESPUINO: "ESPuino – Karten vom Gerät oder aus dem Mediahub",
}


def card_assigned_in(state, card_id: str, folders: list, in_folder) -> bool:
    """Ist die Karte auf dem ESPuino einem der Ordner (oder etwas darin) zugewiesen?"""
    entry = state.assignments.get(card_id) if state is not None else None
    return entry is not None and any(in_folder(entry[0], f) for f in folders)

class MediahubMixin:
    """Methoden von MainWindow (siehe Modul-Doku)"""

    # main.py setzt das: beim Start nicht sofort im WLAN fragen (ein ausgeschalteter
    # ESPuino wuerde den Start aufhalten), sondern nach dem Anzeigen fragen, womit
    # verbunden werden soll (espuino_start_session). Tests verbinden sofort.
    espuino_ask_on_start = False

    def _espuino_check_sd_soon(self):
        """Nur im ESPuino-Modus mit ESPuino direkt und eingetragener SD-Karte"""
        if not self._is_espuino():
            return
        source, _host, sd_root = dev.settings()
        if source == dev.SOURCE_DEVICE and sd_root:
            dev.check_sd_in_background(sd_root)

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
        self._espuino_connected = not self.espuino_ask_on_start
        self._espuino_known = None
        self._espuino_state = None
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
                       self.action_wipe_card, self.action_tag_lookup):
            action.setVisible(False)
        # Kachelmenue: statt TonUINO-Schema (01/001.mp3) ein Ordner mit Albumnamen auf der ESPuino-SD-Karte
        self.library_view.transfer_label = "Auf den ESPuino übertragen"
        self.library_view.transfer_hint = "Zuerst unter „ESPuino einrichten“ den ESPuino direkt im WLAN wählen"
        self.library_view.transfer_enabled = self._espuino_can_transfer
        self.library_view.transfer_requested.disconnect()
        self.library_view.transfer_requested.connect(self._espuino_transfer)
        self._rewire(self.action_card_cover, "Cover-Manager für diese Karte …", self._mediahub_open_cover)
        self._rewire(self.action_card_cover_batch, "Kartencover für alle Karten ohne Cover …",
                     self._mediahub_generate_missing)
        self._rewire(self.action_print_sheet, "Druckbogen (PDF) erstellen …", self._mediahub_print_sheet)
        self.action_series_cover_batch.setText("Kartencover für alle Karten einer Serie ohne Cover …")
        self.action_mediahub_setup = QAction("ESPuino einrichten …", self)
        self.action_mediahub_setup.triggered.connect(self._mediahub_setup)
        self.settings_menu.insertAction(self.action_system_files, self.action_mediahub_setup)
        self.action_use_acr122u = QAction("ACR122U-Leser zum Auslesen nutzen", self)
        self.action_use_acr122u.setCheckable(True)
        self.action_use_acr122u.setChecked(dev.use_acr122u())
        self.action_use_acr122u.setToolTip("USB-Leser ACR122U: Karten auflegen, ohne dass der ESPuino sie abspielt")
        self.action_use_acr122u.toggled.connect(self._espuino_set_acr122u)
        self.settings_menu.insertAction(self.action_system_files, self.action_use_acr122u)

        # Ob die SD-Karte im Kartenleser steckt, im Hintergrund pruefen (ein leerer
        # Kartenleser braucht dafuer Sekunden, siehe dev.sd_available)
        self._espuino_sd_timer = QTimer(self)
        self._espuino_sd_timer.setInterval(30_000)
        self._espuino_sd_timer.timeout.connect(self._espuino_check_sd_soon)
        self._espuino_sd_timer.start()
        self._espuino_check_sd_soon()

        self.mediahub_sidebar = MediahubSidebar()
        self.mediahub_sidebar.setup_requested.connect(self._mediahub_setup)
        self.mediahub_sidebar.reload_requested.connect(self._mediahub_reload)
        self.mediahub_sidebar.assign_requested.connect(self._espuino_assign)
        self.mediahub_sidebar.card_selected.connect(self._mediahub_on_card_clicked)
        sidebar_layout = self.folder_list.parentWidget().layout()
        sidebar_layout.insertWidget(sidebar_layout.indexOf(self.folder_list), self.mediahub_sidebar, 1)
        self.espuino_sidebar = EspuinoDeviceSidebar()
        self.espuino_sidebar.setup_requested.connect(self._mediahub_setup)
        self.espuino_sidebar.connect_requested.connect(self.espuino_connect)
        self.espuino_sidebar.reload_requested.connect(self._mediahub_reload)
        self.espuino_sidebar.assign_requested.connect(self._espuino_read_cards)
        self.espuino_sidebar.assign_folder_requested.connect(self._espuino_assign_folder)
        self.espuino_sidebar.folder_selected.connect(self._espuino_show_folder)
        self.espuino_sidebar.albums_dropped.connect(self._espuino_transfer)
        self.espuino_sidebar.delete_folders_requested.connect(self._espuino_delete_folders)
        self.espuino_sidebar.normalize_folders_requested.connect(self._espuino_normalize_folders)
        self.espuino_sidebar.can_normalize = self._espuino_can_normalize
        self.espuino_sidebar.folder_list.album_names = lambda paths: album_folder_names(paths,
                                                                                         self.library_view.index)
        sidebar_layout.insertWidget(sidebar_layout.indexOf(self.folder_list), self.espuino_sidebar, 1)

        self.mediahub_page = MediahubPage()
        self.mediahub_page.setup_requested.connect(self._mediahub_setup)
        self.mediahub_page.cover_requested.connect(self._mediahub_open_cover)
        self.mediahub_page.batch_requested.connect(self._mediahub_generate_missing)
        self.mediahub_page.series_batch_requested.connect(self._generate_series_card_covers)
        self.mediahub_page.print_requested.connect(self._mediahub_print_sheet)
        self.mediahub_page.transfer_requested.connect(self._espuino_transfer_card)
        self.mediahub_page.back_requested.connect(self._espuino_back)
        self.mediahub_page.cards_table.card_selected.connect(self._mediahub_show_card)
        self.mediahub_page.folder_page.delete_requested.connect(lambda f: self._espuino_delete_folders([f]))
        self.mediahub_page.folder_page.delete_file_requested.connect(self._espuino_delete_folder_file)
        self.mediahub_page.delete_folder_requested.connect(lambda f: self._espuino_delete_folders([f]))
        self.mediahub_page.delete_file_requested.connect(self._espuino_delete_card_file)
        self.mediahub_page.remove_card_requested.connect(lambda cid: self._espuino_remove_cards([cid]))
        self.mediahub_page.cards_table.remove_requested.connect(self._espuino_remove_cards)
        self.mediahub_page.folder_page.card_selected.connect(self._mediahub_show_card)
        self.mediahub_page.folder_page.assign_requested.connect(self._espuino_assign_folder)
        self.mediahub_page.folder_page.cover_requested.connect(self._espuino_folder_cover)
        self.mediahub_page.sibling_selected.connect(self._mediahub_show_card)
        self.mediahub_page.assign_folder_requested.connect(self._espuino_assign_folder)
        self.mediahub_page.cards_table.transfer_selected_requested.connect(
            lambda ids: self._espuino_transfer_cards([c for c in map(self._mediahub_card, ids) if c is not None]))
        self._espuino_setup_player()
        self.stack.addWidget(self.mediahub_page)
        self._sd_page = self.mediahub_page
        # Bibliothek vorn und beim Start offen, die Karten dahinter
        self.view_switch.blockSignals(True)
        self.view_switch.moveTab(self.tab_library, self.tab_sd)
        self.tab_sd, self.tab_library = 1, 0
        self.view_switch.setCurrentIndex(self.tab_library)
        self.view_switch.blockSignals(False)
        self.view_switch.setTabToolTip(self.tab_sd, "Karten des ESPuino: Kartencover gestalten und drucken")
        self.view_switch.setTabToolTip(self.tab_library, "Hörspiele und Musik auf PC und NAS – Album-Cover und Tags")
        self._on_view_switched(self.tab_library)
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
        if self._is_espuino() and index == self.tab_sd:
            self.stack.setCurrentWidget(self.mediahub_page)
            return
        super()._on_view_switched(index)

    def _show_welcome(self):
        if self._is_espuino():
            self._sd_page = self.mediahub_page
            if self.view_switch.currentIndex() == self.tab_sd:
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
                self._espuino_connected = dialog.reachable  # aus: nur die SD-Karte im Kartenleser
            else:
                dev.save_settings(dev.SOURCE_MEDIAHUB, host, sd_root)
                mh.save_settings_paths(dialog.db_path, dialog.media_root)
            self._mediahub_reload()

    def espuino_start_session(self):
        """Nach dem Anzeigen des Fensters (main.py): mit welchem ESPuino verbinden?"""
        if not self._is_espuino() or self._espuino_connected or dev.settings()[0] != dev.SOURCE_DEVICE:
            return
        self.espuino_connect(startup=True)

    def espuino_connect(self, startup: bool = False):
        """Dialog "Mit ESPuino verbinden"; Abbrechen beim Start: ohne Geraet weiter"""
        from gui.espuino_connect_dialog import ConnectDialog
        dialog = ConnectDialog(self, auto_connect=startup and not dev.ask_on_start())
        accepted = dialog.exec()
        host = dialog.selected_host
        dialog.deleteLater()
        if accepted and host:
            dev.set_current(host)
            self._espuino_connected = True
        elif not startup:
            if dev.find_device(self._espuino_host) is not None:
                return  # abgebrochen: alles bleibt, wie es war
            dev.set_current("")  # das aktuelle Geraet wurde entfernt
            self._espuino_connected = False
        self._mediahub_reload()

    def _espuino_restore_reader(self):
        """ESPuino-Modus (aus MainWindow._restore_reader_mode): als Leser gilt immer der
        ACR122U (der TonUINO-Weg passt hier nicht), abgefragt nur, wenn unter
        Einstellungen eingeschaltet"""
        self.rfid_panel.set_reader_mode(self.rfid_panel.READER_MODE_ACR122U)
        self.rfid_panel.set_acr122u_enabled(dev.use_acr122u())

    def _espuino_set_acr122u(self, enabled: bool):
        dev.set_use_acr122u(enabled)
        self.rfid_panel.set_acr122u_enabled(enabled)
        reader = getattr(self, "_espuino_reader", None)
        if reader is None:
            return
        poller = self.rfid_panel._rfid_poller
        if enabled:
            reader.reader_off("wird gesucht …")
            poller.status_changed.connect(reader.on_reader_status)
        else:
            try:
                poller.status_changed.disconnect(reader.on_reader_status)
            except TypeError:
                pass
            reader.reader_off()

    def _espuino_is_device(self) -> bool:
        return self._espuino_source == dev.SOURCE_DEVICE

    def _espuino_place(self) -> str:
        return "auf der SD-Karte" if self._espuino_is_device() else "in der Mediathek"

    def _mediahub_reload(self):
        source, host, sd_root = dev.settings()
        self._espuino_source, self._espuino_host = source, host
        sidebar, page = self.mediahub_sidebar, self.mediahub_page
        device = source == dev.SOURCE_DEVICE
        self.view_switch.setTabText(self.tab_sd, "ESPuino-Karten" if device else "Mediahub-Karten")
        page.overview_title.setText("ESPuino-Karten" if device else "Mediahub-Karten")
        page.device_mode = device
        sidebar.setVisible(not device)
        self.espuino_sidebar.setVisible(device)
        if device:
            self._espuino_reload_device(host, sd_root)
            return
        db_path, root = mh.settings_paths()
        configured = bool(db_path)
        sidebar.btn_assign.setVisible(False)
        sidebar.btn_reload.setEnabled(configured)
        self._mediahub_media_root = root
        if not configured:
            self._mediahub_data = None
            sidebar.set_cards([], {})
            page.show_overview("Noch nicht eingerichtet: die db.json des ESPuino-Mediahub und die Mediathek "
                               "wählen. Studio liest die Karten nur, am Mediahub ändert sich nichts.",
                               configured=False)
            return
        try:
            data = mh.load_cards(db_path)
        except mh.MediahubError as exc:
            log.warning("Mediahub-Karten: %s", exc)
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
        self._status(f"{len(data.cards)} Karten aus dem Mediahub eingelesen")

    # --- ESPuino direkt: bekannte Karten und Geraet ---------------------------------

    def _espuino_data_dir(self) -> str:
        archive = get_archive(self, ask=False)
        return str(archive.root) if archive is not None else ""

    def _espuino_reload_device(self, host: str, sd_root: str):
        page = self.mediahub_page
        self._mediahub_media_root = sd_root
        known = espuino_cards.load(self._espuino_data_dir())
        if not host and not known.cards and not dev.saved_devices():
            self._mediahub_data = None
            self._espuino_state = None
            self.espuino_sidebar.set_state("", None, {})
            page.show_overview("Noch nicht eingerichtet: die Adresse des ESPuino im WLAN wählen, dazu am besten "
                               "seine SD-Karte im Kartenleser. Studio liest dann seine Karten und kann neue "
                               "zuweisen.", configured=False)
            return
        if host:
            state = dev.fetch_state(host, sd_root, connect=self._espuino_connected)
        else:  # bekannte Karten ohne Geraet
            state = dev.DeviceState(host="", error=dev.NOT_CONNECTED)
        data, changed = dev.build_cards(known, state, self.library_view.index, sd_root)
        if changed:
            try:
                known.save()
            except OSError as exc:
                log.warning("Kartenverzeichnis nicht gespeichert: %s", exc)
        self._espuino_known, self._espuino_state, self._mediahub_data = known, state, data
        folder_cards = {}
        for card_id, (target, _mode) in state.assignments.items():
            card = known.get(card_id)
            folder_cards.setdefault(target.strip("/").casefold(), []).append(card.label if card else card_id)
        device = dev.find_device(host)
        self.espuino_sidebar.set_state(host, state, folder_cards, device.label if device else "",
                                       self._espuino_folder_series(state.folders or []))
        self.library_view.set_device_map(self._espuino_device_albums(state, data.cards))
        has_cover = self._mediahub_cover_map()
        # Uebertragen geht auch ohne ESPuino, wenn seine SD-Karte im PC steckt
        page.cards_table.set_cards(data.cards, has_cover, state.reachable or state.folders_from_pc,
                                   {c.card_id: self._mediahub_card_series(c) for c in data.cards})
        if page.card is not None and mh.card_by_id(data, page.card.card_id) is not None:
            self._mediahub_show_card(page.card.card_id)
        else:
            self._mediahub_show_overview(has_cover)
        reader = getattr(self, "_espuino_reader", None)
        if reader is not None:
            reader.set_device(host if state.reachable else "", self._espuino_folder_choices(), state.folders or [],
                              self._espuino_folder_notes())
        offline = "" if state.reachable else (" – ohne ESPuino" if not self._espuino_connected
                                              else " – ESPuino nicht erreichbar")
        self._status(f"{len(data.cards)} bekannte Karten" + offline)

    def _espuino_back(self):
        self.mediahub_page.card = None
        self._mediahub_show_overview(self._mediahub_cover_map())

    def _espuino_album_for_folder(self, sd_folder: str):
        return espuino_cards.find_library_album(espuino_cards.KnownCard("", sd_folder=sd_folder),
                                                self.library_view.index)

    def _espuino_assign(self, preset_card: str = "", preset_path: str = "", album=None):
        """Karte zuweisen: oeffnet das Fenster "Karten auslesen und zuweisen", vorbelegt"""
        reader = self._espuino_read_cards()
        if reader is not None:
            reader.preset(card_id=preset_card, folder=preset_path, album=album)

    def _espuino_on_assigned(self, card_id: str, folder: str, play_mode: int, album=None):
        """Das Fenster hat eine Karte auf dem ESPuino zugewiesen: im Verzeichnis merken"""
        known = getattr(self, "_espuino_known", None) or espuino_cards.load(self._espuino_data_dir())
        if album is None:
            album = self._espuino_album_for_folder(folder)
        known.remember(card_id, name=album.name if album is not None else folder.strip("/").rpartition("/")[2],
                       sd_folder=folder, play_mode=play_mode,
                       library_path=album.path if album is not None else "",
                       album_id=album.album_id if album is not None else "")
        try:
            known.save()
        except OSError as exc:
            log.warning("Kartenverzeichnis nicht gespeichert: %s", exc)
        state = self._espuino_state
        done = "zugewiesen" if state is not None and state.reachable else "vorgemerkt"
        self._status(f"Karte {mh.format_card_id(card_id)} {done}: {folder}")
        self._mediahub_reload()
    def _espuino_library_albums(self) -> list:
        """Alben der Bibliothek, die sich auf den ESPuino bringen lassen"""
        from core.library import in_system_folder
        index = self.library_view.index
        return [a for a in (index.albums if index is not None else [])
                if a.tracks and not a.offline and not in_system_folder(a.path)]

    def _espuino_transfer_and_assign(self, card_id: str, album, folder: str, play_mode: int):
        """Aus "Karten auslesen und zuweisen": Album fehlt auf dem ESPuino - erst
        uebertragen (fehlende Dateien), dann die Karte zuweisen"""
        reader = getattr(self, "_espuino_reader", None)
        if self._espuino_transfer_busy() or not album.tracks:
            return
        _source, host, sd_root = dev.settings()
        local = sd_root if sd_root and dev.sd_available(sd_root) else ""
        state = self._espuino_state
        assignment = (card_id, play_mode) if state is not None and state.reachable else None  # sonst vormerken

        def finished(cancelled):
            if cancelled:
                self._mediahub_reload()
                if reader is not None:
                    reader.assign_note.setText("Übertragen abgebrochen – Karte nicht zugewiesen.")
                return
            self._espuino_on_assigned(card_id, folder, play_mode, album)
            if getattr(self, "_espuino_reader", None) is reader and reader is not None:
                reader.assignment_done(card_id, folder)

        self._espuino_run_transfer([plan_album(album.tracks, folder)], local, host, [assignment], finished)

    def _espuino_device_albums(self, state, cards) -> dict:
        """Albumpfad -> Ordner auf der SD-Karte des ESPuino (fuer das Badge "auf ESPuino" in der Bibliothek)"""
        result = {}
        for card in cards:
            if card.library_path and card.folder_present:
                result[card.library_path] = card.sd_path
        for folder in state.folders or []:
            album = self._espuino_album_for_folder(folder)
            if album is not None:
                result.setdefault(album.path, folder)
        return result

    def _espuino_folder_series(self, folders: list) -> dict:
        """Ordner (klein) -> Serie: das Serienprofil des Albums im Archiv, sonst der
        Interpret des Bibliotheksalbums (etwa "Bibi und Tina", "Conni")"""
        archive = get_archive(self, ask=False)
        result = {}
        for folder in folders:
            cards = self._espuino_folder_cards(folder)
            series = ""
            if archive is not None:
                for card in cards:
                    record = archive.load_album(card.album_id) if card.album_id else None
                    profile = archive.series.load(record.series_id) if record is not None and record.series_id \
                        else None
                    if profile is not None and profile.name:
                        series = profile.name
                        break
            if not series:
                album = next((self.library_view.index.find(c.library_path) for c in cards if c.library_path), None) \
                    or self._espuino_album_for_folder(folder)
                series = (album.artist or "").strip() if album is not None else ""
            if series:
                result[folder.casefold()] = series
        return result

    def _mediahub_card_series(self, card) -> str:
        """Serie einer Karte wie in der Bibliothek: Serienprofil des Albums im Archiv,
        sonst der Interpret des Bibliotheksalbums"""
        archive = get_archive(self, ask=False)
        if archive is not None and card.album_id:
            record = archive.load_album(card.album_id)
            profile = archive.series.load(record.series_id) if record is not None and record.series_id else None
            if profile is not None and profile.name:
                return profile.name
        album = self.library_view.index.find(card.library_path) if card.library_path else None
        if album is None and card.sd_path:
            album = self._espuino_album_for_folder(card.sd_path)
        return (album.artist or "").strip() if album is not None else ""

    def _espuino_folder_notes(self) -> dict:
        """Ordner (klein) -> Namen der Karten, die darauf zeigen (fuer die Auswahl beim Zuweisen)"""
        notes = {}
        data = self._mediahub_data
        for card in data.cards if data else []:
            for folder in {card.sd_path, card.device_target} - {""}:
                names = notes.setdefault(folder.casefold(), [])
                label = f"Karte: {card.label}"
                if label not in names:
                    names.append(label)
        return {k: ", ".join(v) for k, v in notes.items()}

    def _espuino_folder_choices(self) -> list:
        """Ordner fuer die Auswahlliste beim Zuweisen: auf dem ESPuino und die der bekannten Karten"""
        state = self._espuino_state
        folders = set(state.folders or []) if state is not None else set()
        data = self._mediahub_data
        folders.update(c.sd_path for c in (data.cards if data else []) if c.sd_path)
        return sorted(folders, key=str.casefold)

    # --- Karten auslesen und zuweisen ------------------------------------------------

    def _espuino_read_cards(self):
        """Fenster, das jede aufgelegte Karte zeigt und zuweisen laesst (nicht modal).
        Gibt das (ggf. schon offene) Fenster zurueck."""
        from gui.espuino_card_reader import CardReaderDialog
        reader = getattr(self, "_espuino_reader", None)
        if reader is not None:
            reader.raise_()
            reader.activateWindow()
            return reader
        if not self._espuino_is_device():
            return None
        state = self._espuino_state
        host = self._espuino_host if state is not None and state.reachable else ""
        _source, _host, sd_root = dev.settings()
        reader = CardReaderDialog(host, self._espuino_card_info, self, folders=self._espuino_folder_choices(),
                                  sd_root=sd_root)
        poller = getattr(getattr(self, "rfid_panel", None), "_rfid_poller", None)
        if not dev.use_acr122u() or poller is None:
            reader.reader_off()
        else:
            poller.status_changed.connect(reader.on_reader_status)  # loest Qt beim Schliessen selbst
        reader.show_card_requested.connect(self._espuino_reader_show_card)
        reader.assigned.connect(self._espuino_on_assigned)
        reader.transfer_requested.connect(self._espuino_transfer_and_assign)
        reader.set_albums(self._espuino_library_albums())
        reader.set_device(host, self._espuino_folder_choices(),
                          (state.folders or []) if state is not None else [], self._espuino_folder_notes())
        reader.destroyed.connect(lambda *_: setattr(self, "_espuino_reader", None))
        self._espuino_reader = reader
        reader.show()
        return reader

    def _espuino_reader_show_card(self, card_id: str):
        if mh.card_by_id(self._mediahub_data, card_id) is not None:
            self.view_switch.setCurrentIndex(self.tab_sd)
            self._mediahub_show_card(card_id)

    def _espuino_album_thumb(self, album) -> str:
        """Vorschaubild eines Bibliotheksalbums ("" ohne Bild oder Bibliothek)"""
        library = getattr(self.library_view, "library", None)
        path = library.thumb_path(album) if library is not None else None
        return str(path) if path else ""

    def _espuino_card_info(self, card_id: str):
        """Alles, was Studio zu einer Kartennummer weiss (fuer "Karte auslesen")"""
        from core import card_store
        from gui.espuino_card_reader import CardInfo
        info = CardInfo(card_id)
        state = self._espuino_state
        entry = state.assignments.get(card_id) if state is not None else None
        if entry is not None:
            info.assignment = entry[0]
            info.mode = int(entry[1])
            info.play_mode = mh.play_mode_label(str(entry[1]))
        card = mh.card_by_id(self._mediahub_data, card_id)
        if card is None:
            if entry is not None:
                info.status = "Seit dem letzten Einlesen neu zugewiesen – „Neu einlesen“ zeigt mehr."
            return info
        info.known, info.name, info.known_folder = True, card.label, card.sd_path
        if not info.play_mode and card.play_mode:
            info.play_mode = mh.play_mode_label(card.play_mode)
        if not info.mode and str(card.play_mode).isdigit():
            info.mode = int(card.play_mode)
        if entry is None and card.sd_path:
            info.status = f"Gehört zu {card.sd_path}, ist aber auf diesem ESPuino nicht zugewiesen."
        elif entry is not None and card.sd_path and not dev.same_folder(entry[0], card.sd_path):
            info.status = f"Studio kennt sie als {card.sd_path}."
        elif card.folder_present is False:
            info.status = "Der Ordner fehlt auf der SD-Karte des ESPuino."
        album = self.library_view.index.find(card.library_path) if card.library_path else None
        if album is not None:
            info.album = album.name
            minutes = int(round(album.duration / 60)) if album.duration else 0
            info.album_detail = f"{album.track_count} Titel" + (f", {minutes} min" if minutes else "")
            info.album_cover_path = self._espuino_album_thumb(album)
        archive = get_archive(self, ask=False)
        if archive is not None:
            card_dir = str(archive.card_dir(card.album_id))
            front, back = card_store.card_paths(card_dir)[0], card_store.back_paths(card_dir)[0]
            info.front_path = str(front) if front.is_file() else ""
            info.back_path = str(back) if back.is_file() else ""
        return info

    def _espuino_folder_cards(self, folder: str) -> list:
        """Bekannte Karten, die auf diesen Ordner (oder darin) zeigen"""
        from gui.espuino_device_view import in_folder
        data = self._mediahub_data
        if not folder or data is None:
            return []
        return [c for c in data.cards if in_folder(c.sd_path, folder) or in_folder(c.device_target, folder)]

    def _espuino_show_folder(self, folder: str):
        """Ordner der SD-Karte: Album aus der Bibliothek und alle Karten, die darauf zeigen"""
        data = self._mediahub_data
        if not folder or data is None:
            return
        cards = self._espuino_folder_cards(folder)
        if cards:  # Ordner mit Karte: dieselbe Ansicht wie die Karte, darunter alle Karten des Ordners
            here = [c for c in cards if c.device_target and dev.same_folder(c.device_target, folder)]
            self._mediahub_show_card((here or cards)[0].card_id)
            if self.view_switch.currentIndex() != self.tab_sd:
                self.view_switch.setCurrentIndex(self.tab_sd)
            return
        # Ordner ohne Karte
        meta = []
        album = self._espuino_album_for_folder(folder)
        preview, files = "", None
        if album is not None:
            minutes = int(round(album.duration / 60)) if album.duration else 0
            meta.append(f"Album: {album.name}" + (f" – {album.artist}" if album.artist else ""))
            meta.append(f"{album.track_count} Titel" + (f", {minutes} min" if minutes else "")
                        + f" · Bibliothek: {album.path}")
            preview = self._espuino_album_thumb(album)
            files = [os.path.relpath(track, album.path) if os.path.isabs(track) else track for track in album.tracks]
            try:
                sd_files = [i.target for i in plan_album(album.tracks, folder).items]
            except dev.DeviceError:
                sd_files = []
        else:
            meta.append("Kein passendes Album in der Bibliothek gefunden.")
            _source, _host, sd_root = dev.settings()
            if sd_root and dev.sd_available(sd_root):
                found, _ok = dev.card_files(sd_root, folder, 3)
                files = found if _ok else []
            sd_files = ["/" + f.strip("/") for f in files or []]
        state = self._espuino_state
        reachable = state is not None and state.reachable
        self._espuino_folder = (folder, album, [])
        self._espuino_folder_files = sd_files
        can_delete = self._espuino_delete_target() is not None
        self.mediahub_page.show_folder(folder, meta, preview, [], {}, reachable, album is not None, files,
                                       can_delete=can_delete)
        locals_ = list(album.tracks) if album is not None else []
        entries = [(sd_files[i] if i < len(sd_files) else "", locals_[i] if i < len(locals_) else "",
                    os.path.basename(name.replace("\\", "/").rstrip("/")) or name)
                   for i, name in enumerate(files or [])]
        self._espuino_set_play_entries(self.mediahub_page.folder_page.file_list, entries)
        self.mediahub_page.folder_page.set_series(self._espuino_folder_series([folder]).get(folder.casefold(), ""))
        if self.view_switch.currentIndex() != self.tab_sd:
            self.view_switch.setCurrentIndex(self.tab_sd)

    def _espuino_folder_cover(self, folder: str):
        """Cover-Manager aus der Ordnerseite: fuer das Bibliotheksalbum, sonst fuer die erste Karte"""
        shown, album, card_ids = getattr(self, "_espuino_folder", ("", None, []))
        if shown != folder:
            return
        if album is not None:
            self._library_cover(album, focus=CoverManagerDialog.FOCUS_CARD)
            self._mediahub_reload()
        elif card_ids:
            self._mediahub_open_cover(card_ids[0])
        self._espuino_show_folder(folder)  # neues Kartencover gleich zeigen

    def _espuino_assign_folder(self, folder: str):
        self._espuino_assign(preset_path=folder, album=self._espuino_album_for_folder(folder))

    # --- Loeschen: Ordner und Dateien auf der SD-Karte, Karten ---------------------------

    def _espuino_delete_target(self):
        """(sd_root, host) zum Loeschen - wie beim Uebertragen die SD-Karte im PC zuerst,
        sonst das WLAN; None, wenn keins von beiden geht"""
        _source, host, sd_root = dev.settings()
        if sd_root and dev.sd_available(sd_root):
            return sd_root, ""
        state = self._espuino_state
        if host and state is not None and state.reachable:
            return "", host
        return None

    def _espuino_confirm(self, title: str, text: str, ok_text: str, check_text: str = ""):
        """Rueckfrage vor dem Loeschen, "Abbrechen" vorgewaehlt. (ja, Haken gesetzt)"""
        box = QMessageBox(QMessageBox.Icon.Warning, title, text,
                          QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel, self)
        box.button(QMessageBox.StandardButton.Yes).setText(ok_text)
        box.button(QMessageBox.StandardButton.Cancel).setText("Abbrechen")
        box.setDefaultButton(QMessageBox.StandardButton.Cancel)
        check = None
        if check_text:
            check = QCheckBox(check_text)
            box.setCheckBox(check)
        answer = box.exec()
        return answer == QMessageBox.StandardButton.Yes, check is not None and check.isChecked()

    def _espuino_delete_folders(self, folders: list):
        """Ordner samt Inhalt von der SD-Karte loeschen (Seitenleiste, Ordner- und Kartenseite)"""
        from core import espuino_transfer as et
        from gui.espuino_device_view import in_folder
        title = "Von der SD-Karte löschen"
        target = self._espuino_delete_target()
        if target is None:
            QMessageBox.information(self, title, "Löschen geht nur mit verbundenem ESPuino oder mit seiner "
                                    "SD-Karte im Kartenleser.")
            return
        sd_root, host = target
        lines, affected = [], []
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            for folder in folders:
                count, size = et.folder_summary(folder, sd_root, host)
                amount = f"{count} Datei{'' if count == 1 else 'en'}" + (
                    f", {size / 1e6:.0f} MB" if size is not None else "")
                lines.append(f"• {folder}  ({amount})")
                for card in self._espuino_folder_cards(folder):
                    if card.card_id not in {c.card_id for c in affected}:
                        affected.append(card)
        except dev.DeviceError as exc:
            QMessageBox.warning(self, title, str(exc))
            return
        finally:
            QApplication.restoreOverrideCursor()
        state = self._espuino_state
        reachable = state is not None and state.reachable
        assigned = [c for c in affected if reachable and card_assigned_in(state, c.card_id, folders, in_folder)]
        one = len(folders) == 1
        text = (f"{'Diesen Ordner' if one else f'Diese {len(folders)} Ordner'} samt Inhalt endgültig "
                f"{'von der SD-Karte im Kartenleser' if sd_root else 'über das WLAN von der SD-Karte des ESPuino'} "
                "löschen? Das lässt sich nicht rückgängig machen.\n\n" + "\n".join(lines))
        if affected:
            text += (f"\n\n{len(affected)} Karte{' zeigt' if len(affected) == 1 else 'n zeigen'} darauf und "
                     "spiel" + ("t" if len(affected) == 1 else "en") + " danach nichts mehr:\n"
                     + "\n".join(f"• {c.label}" for c in affected[:12]))
        check_text = ""
        if assigned:
            check_text = (f"Auch die Zuweisung{'' if len(assigned) == 1 else 'en'} dieser "
                          f"{len(assigned)} Karte{'' if len(assigned) == 1 else 'n'} auf dem ESPuino entfernen "
                          "(in der App bleiben sie vorgemerkt)")
        ok, unassign = self._espuino_confirm(title, text, "Endgültig löschen", check_text)
        if not ok:
            return
        self._release_files()
        errors, deleted = [], 0
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            for folder in folders:
                try:
                    et.delete_from_sd(folder, sd_root, host)
                    deleted += 1
                except dev.DeviceError as exc:
                    errors.append(str(exc))
            if unassign:
                _source, device_host, _sd = dev.settings()
                for card in assigned:
                    try:
                        dev.unassign_card(device_host, card.card_id)
                    except dev.DeviceError as exc:
                        errors.append(f"{card.label}: {exc}")
        finally:
            QApplication.restoreOverrideCursor()
        self._status(f"{deleted} Ordner gelöscht" + (f", {len(assigned)} Zuweisung"
                                                     f"{'' if len(assigned) == 1 else 'en'} entfernt"
                                                     if unassign else ""))
        if errors:
            QMessageBox.warning(self, title, "\n".join(errors))
        self._mediahub_reload()

    # --- Wiedergabe -----------------------------------------------------------------

    def _espuino_setup_player(self):
        """Dateilisten der Karten- und Ordnerseite spielen per Doppelklick im Player"""
        self._espuino_play_lists = {}  # id(Liste) -> [(SD-Pfad, lokale Datei, Titel)] je Zeile
        self._espuino_now = None  # (Liste, Zeile) des geladenen Tracks
        for file_list in (self.mediahub_page.file_list, self.mediahub_page.folder_page.file_list):
            file_list.itemDoubleClicked.connect(
                lambda item, lst=file_list: self._espuino_play_row(lst, lst.row(item), toggle=True))
            file_list.currentRowChanged.connect(lambda _row: self._espuino_update_idle())
            file_list.setToolTip("Doppelklick spielt die Datei ab")

    def _espuino_set_play_entries(self, file_list, entries: list):
        self._espuino_play_lists[id(file_list)] = entries
        if self._espuino_now is not None and self._espuino_now[0] is file_list:
            self._espuino_now = None  # Liste neu aufgebaut: Zeilen passen nicht mehr
        self._espuino_update_player_navigation()
        self._espuino_update_idle()

    def _espuino_play_source(self, sd_path: str, local: str = "") -> str:
        """Was der Player spielen kann: die Datei der Bibliothek, sonst die SD-Karte
        im Kartenleser, sonst der Download vom ESPuino ueber WLAN; "" wenn nichts geht"""
        if local and os.path.isfile(local):
            return local
        if not sd_path or not self._espuino_is_device():
            return ""
        _source, host, sd_root = dev.settings()
        if sd_root and dev.sd_available(sd_root):
            path = os.path.join(sd_root, *sd_path.strip("/").split("/"))
            if os.path.isfile(path):
                return path
        state = self._espuino_state
        if host and state is not None and state.reachable:
            return dev.download_url(host, sd_path)
        return ""

    def _espuino_play_row(self, file_list, row: int, toggle: bool = False):
        entries = self._espuino_play_lists.get(id(file_list), [])
        if not 0 <= row < len(entries):
            return
        sd_path, local, title = entries[row]
        source = self._espuino_play_source(sd_path, local)
        if not source:
            self._status("Abspielen geht, wenn das Album in der Bibliothek liegt, die SD-Karte im PC "
                         "steckt oder der ESPuino verbunden ist.")
            return
        self._espuino_now = (file_list, row)
        if toggle:
            self.player_bar.toggle_track(source, title)
        else:
            self.player_bar.load_track(source, title, autoplay=True)
        file_list.setCurrentRow(row)
        self._espuino_update_player_navigation()

    def _espuino_visible_file_list(self):
        page = self.mediahub_page
        current = page.pages.currentWidget()
        for file_list in (page.file_list, page.folder_page.file_list):
            if current is not None and current.isAncestorOf(file_list):
                return file_list
        return None

    def _espuino_play_selected(self):
        file_list = self._espuino_visible_file_list()
        if file_list is not None:
            self._espuino_play_row(file_list, max(0, file_list.currentRow()))

    def _espuino_play_relative(self, delta: int):
        if self._espuino_now is None:
            return
        file_list, row = self._espuino_now
        self._espuino_play_row(file_list, row + delta)

    def _espuino_update_player_navigation(self):
        now = getattr(self, "_espuino_now", None)
        if now is None or not hasattr(self, "player_bar"):
            if hasattr(self, "player_bar"):
                self.player_bar.set_navigation_enabled(False, False)
            return
        file_list, row = now
        count = len(self._espuino_play_lists.get(id(file_list), []))
        self.player_bar.set_navigation_enabled(row > 0, row < count - 1)

    def _espuino_update_idle(self):
        file_list = self._espuino_visible_file_list()
        entries = self._espuino_play_lists.get(id(file_list), []) if file_list is not None else []
        self.player_bar.set_idle_playable(bool(entries))

    def _espuino_card_play_entries(self, card, folder) -> list:
        """Zeilen der Dateiliste einer Karte: SD-Pfad (nur ESPuino direkt), lokale Datei, Titel"""
        sd_paths = self._espuino_card_sd_files(card) if self._espuino_is_device() else []
        album = self.library_view.index.find(card.library_path) if card.library_path else None
        album_tracks = list(album.tracks) if album is not None and album.tracks else []
        by_name = {os.path.basename(t.filepath).casefold(): t.filepath for t in folder.tracks}
        entries = []
        for i, rel in enumerate(card.files):
            if len(album_tracks) == len(card.files):
                local = album_tracks[i]
            else:
                local = by_name.get(os.path.basename(rel).casefold(), "")
            sd_path = sd_paths[i] if i < len(sd_paths) else ""
            entries.append((sd_path, local, os.path.basename(rel.rstrip("/")) or rel))
        return entries

    def _espuino_card_sd_files(self, card) -> list:
        """Pfade auf der SD-Karte zu den Zeilen der Dateiliste einer Karte"""
        from gui.espuino_device_view import can_transfer
        album = self.library_view.index.find(card.library_path) if card.library_path else None
        if album is not None and album.tracks and can_transfer(card):
            try:
                targets = [i.target for i in plan_album(album.tracks, card.sd_path).items]
            except dev.DeviceError:
                targets = []
            if len(targets) == len(card.files):
                return targets
        return ["/" + f.strip("/") for f in card.files]

    def _espuino_delete_card_file(self, card_id: str, row: int):
        card = self._mediahub_card(card_id)
        paths = self._espuino_card_sd_files(card) if card is not None else []
        if 0 <= row < len(paths):
            self._espuino_delete_file(paths[row], lambda: self._mediahub_show_card(card_id))

    def _espuino_delete_folder_file(self, row: int):
        paths = getattr(self, "_espuino_folder_files", [])
        folder = getattr(self, "_espuino_folder", ("",))[0]
        if 0 <= row < len(paths):
            self._espuino_delete_file(paths[row], lambda: self._espuino_show_folder(folder))

    def _espuino_delete_file(self, sd_path: str, refresh):
        """Eine Datei von der SD-Karte loeschen; vorher nachsehen, ob es sie dort gibt"""
        from core import espuino_transfer as et
        title = "Von der SD-Karte löschen"
        target = self._espuino_delete_target()
        if target is None:
            QMessageBox.information(self, title, "Löschen geht nur mit verbundenem ESPuino oder mit seiner "
                                    "SD-Karte im Kartenleser.")
            return
        sd_root, host = target
        try:
            present = et.exists_on_sd(sd_path, sd_root, host)
        except dev.DeviceError as exc:
            QMessageBox.warning(self, title, str(exc))
            return
        if not present:
            QMessageBox.information(self, title, f"{sd_path} liegt nicht auf der SD-Karte – nichts zu löschen.")
            return
        ok, _ = self._espuino_confirm(title, f"Diese Datei endgültig von der SD-Karte löschen?\n\n{sd_path}\n\n"
                                      "Das lässt sich nicht rückgängig machen. Mit „Ausgewählte übertragen“ "
                                      "lässt sie sich aus der Bibliothek wieder ergänzen.", "Endgültig löschen")
        if not ok:
            return
        self._release_files()
        try:
            et.delete_from_sd(sd_path, sd_root, host)
        except dev.DeviceError as exc:
            QMessageBox.warning(self, title, str(exc))
            return
        self._status(f"Gelöscht: {sd_path}")
        self._mediahub_reload()
        refresh()

    def _espuino_remove_cards(self, card_ids: list):
        """Karten entfernen: Zuweisung auf diesem ESPuino loeschen und aus dem
        Kartenverzeichnis der App nehmen. Ordner, Dateien und Cover bleiben."""
        title = "Karten entfernen"
        cards = [c for c in (self._mediahub_card(i) for i in card_ids) if c is not None]
        if not cards:
            return
        state = self._espuino_state
        reachable = state is not None and state.reachable
        on_device = [c for c in cards if state is not None and c.card_id in state.assignments]
        if on_device and not reachable:
            QMessageBox.information(self, title, "Diese Karten sind auf dem ESPuino zugewiesen (laut backup.txt). "
                                    "Zum Entfernen erst mit dem ESPuino verbinden – sonst kämen sie beim nächsten "
                                    "Einlesen zurück:\n" + "\n".join(f"• {c.label}" for c in on_device[:12]))
            cards = [c for c in cards if c not in on_device]
            on_device = []
            if not cards:
                return
        n = len(cards)
        text = (f"{'Diese Karte' if n == 1 else f'Diese {n} Karten'} entfernen?\n\n"
                + "\n".join(f"• {c.label}  ({mh.format_card_id(c.card_id)})" for c in cards[:12])
                + ("\n\nAuf diesem ESPuino wird die Zuweisung gelöscht – die Karte spielt dann nichts mehr."
                   if on_device else "")
                + "\n\nDie App vergisst Name und Ordner der Karte. Ordner und Dateien auf der SD-Karte "
                  "und die Kartencover im Archiv bleiben.")
        ok, _ = self._espuino_confirm(title, text, "Entfernen")
        if not ok:
            return
        _source, host, _sd_root = dev.settings()
        known = self._espuino_known
        errors, removed = [], 0
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            for card in cards:
                if card in on_device:
                    try:
                        dev.unassign_card(host, card.card_id)
                    except dev.DeviceError as exc:
                        errors.append(f"{card.label}: {exc}")
                        continue  # bliebe auf dem Geraet und kaeme beim Einlesen zurueck
                known.forget(card.card_id)
                removed += 1
            known.save()
        except OSError as exc:
            errors.append(f"Kartenverzeichnis nicht gespeichert: {exc}")
        finally:
            QApplication.restoreOverrideCursor()
        self._status(f"{removed} Karte{'' if removed == 1 else 'n'} entfernt")
        if errors:
            QMessageBox.warning(self, title, "\n".join(errors))
        self._mediahub_reload()

    # --- Alben auf den ESPuino -----------------------------------------------------

    def _espuino_can_transfer(self) -> bool:
        source, host, sd_root = dev.settings()
        return source == dev.SOURCE_DEVICE and bool((host and self._espuino_connected) or sd_root)

    def _espuino_transfer_busy(self) -> bool:
        worker = getattr(self, "_espuino_transfer_worker", None)
        return worker is not None and worker.isRunning()

    def _espuino_transfer(self, paths: list):
        """Alben aus der Bibliothek (Kachelmenue oder Drag & Drop in die Seitenleiste)"""
        if self._espuino_transfer_busy():
            return
        _source, host, sd_root = dev.settings()
        albums = [a for a in (self.library_view.index.find(p) for p in paths) if a is not None and a.tracks]
        if not albums:
            return
        existing = {a.path: self._espuino_existing_folders(a) for a in albums}
        dialog = TransferDialog(albums, sd_root, host, self, existing=existing,
                                can_normalize=self.audio_converter.is_available)
        if not dialog.exec():
            dialog.deleteLater()
            return
        plans, assign_after, local, complete = dialog.plans, dialog.assign_after, dialog.sd_root, dialog.complete
        normalize = dialog.normalize
        dialog.deleteLater()
        target = plans[0].folder if plans else (complete[0][1] if complete else "")
        if complete:
            self._status("Schon vollständig auf der SD-Karte, nichts übertragen: "
                         + ", ".join(f"{a.name} ({folder})" for a, folder in complete))
        if not plans:  # alles schon da: gleich weiter zum Zuweisen
            if assign_after and target:
                self._espuino_assign(preset_path=target, album=albums[0])
            else:
                QMessageBox.information(self, "Auf den ESPuino übertragen",
                                        "Schon vollständig auf der SD-Karte – nichts zu übertragen:\n"
                                        + "\n".join(f"• {a.name} → {folder}" for a, folder in complete))
            return

        def finished(cancelled):
            if not cancelled and assign_after and target:
                self._espuino_assign(preset_path=target, album=albums[0])
            else:
                self._mediahub_reload()

        self._espuino_run_transfer(plans, local, host, None, finished, normalize=normalize)

    def _espuino_existing_folders(self, album) -> list:
        """Ordner auf der SD-Karte des ESPuino, in denen dieses Album schon liegt:
        gleicher Ordnername wie beim Uebertragen, oder ein Ordner, den die App diesem
        Album zuordnet (Album-ID oder Name, ueber bekannte Karten auch Unterordner)"""
        from core.espuino_transfer import safe_name
        state = self._espuino_state
        if state is None or state.folders is None:  # Inhalt der SD-Karte unbekannt
            return []
        on_card = {f.strip("/").casefold() for f in state.folders}

        def same_album(found) -> bool:
            return found is not None and os.path.normcase(found.path) == os.path.normcase(album.path)

        name = "/" + safe_name(album.name)
        result = [f for f in state.folders
                  if dev.same_folder(f, name) or same_album(self._espuino_album_for_folder(f))]
        known = getattr(self, "_espuino_known", None)
        for card in (known.cards.values() if known is not None else []):
            folder = card.sd_folder
            if (folder and folder.strip("/").split("/")[0].casefold() in on_card
                    and not any(dev.same_folder(folder, f) for f in result)
                    and same_album(espuino_cards.find_library_album(card, self.library_view.index))):
                result.append(folder)  # auch Unterordner, ueber eine bekannte Karte
        return result

    def _espuino_transfer_cards(self, cards: list):
        """Ausgewaehlte Karten auf diesen ESPuino: fehlende Dateien des Albums aus der
        Bibliothek in den bekannten Ordner, vorgemerkte Karten zuweisen. Vorher wird
        geprueft, welche Dateien wirklich fehlen - die Rueckfrage nennt die echte Menge."""
        from core.espuino_cards import ASSIGNED
        from core.espuino_transfer import pending_items
        from gui.espuino_device_view import can_transfer
        if self._espuino_transfer_busy() or not cards:
            return
        state = self._espuino_state
        can_assign = state is not None and state.reachable
        _source, host, sd_root = dev.settings()
        local = sd_root if sd_root and dev.sd_available(sd_root) else ""
        title = "Auf den ESPuino übertragen"
        if not can_assign and not local:
            QMessageBox.information(self, title, "Der ESPuino ist nicht erreichbar, "
                                    "und seine SD-Karte steckt nicht im Kartenleser.")
            return
        normalize = self._espuino_normalize_wanted()
        plans, assignments, transfer_cards = [], [], []
        assign_only, complete, waiting, impossible, errors = [], [], [], [], []
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            for card in cards:
                wants_assign = can_assign and bool(card.sd_path) and card.device_status != ASSIGNED
                files_complete = card.folder_present is True
                album = self.library_view.index.find(card.library_path) if can_transfer(card) else None
                if album is not None and album.tracks:
                    plan = plan_album(album.tracks, card.sd_path)
                    try:
                        plan.items = pending_items(plan, local, "" if local else host, by_name=normalize)
                    except dev.DeviceError as exc:
                        errors.append(f"{card.label}: {exc}")
                        continue
                    if plan.items:
                        plans.append(plan)
                        assignments.append((card.card_id, int(card.play_mode or 3)) if wants_assign else None)
                        transfer_cards.append(card)
                        continue
                    files_complete = True
                if not files_complete:
                    impossible.append(card)
                elif wants_assign:
                    assign_only.append(card)
                elif not can_assign and card.device_status != ASSIGNED:
                    waiting.append(card)  # Dateien da, zugewiesen wird nach dem Verbinden
                else:
                    complete.append(card)
        finally:
            QApplication.restoreOverrideCursor()

        NL = "\n"

        def plural(n, one, many):
            return f"{n} {one if n == 1 else many}"

        def names(items):
            return NL.join(f"   • {c.label}" for c in items[:12]) + (NL + "   • …" if len(items) > 12 else "")

        assign_count = sum(a is not None for a in assignments) + len(assign_only)
        parts = []
        if plans:
            size = sum(p.total for p in plans)
            files = sum(len(p.items) for p in plans)
            amount = f"{size / 1e6:.0f} MB" if size >= 1e6 else f"{max(size, 1) / 1e3:.0f} KB"
            way = "auf die SD-Karte im Kartenleser" if local else "über das WLAN"
            parts.append(f"{plural(len(plans), 'Album', 'Alben')} {way} übertragen – "
                         f"{plural(files, 'fehlende Datei', 'fehlende Dateien')}, {amount}:" + NL + names(transfer_cards))
            if normalize:
                parts.append("Die Lautstärke wird dabei angeglichen (Einstellung aus „Auf den ESPuino übertragen“); "
                             "die Dateien in der Bibliothek bleiben unverändert.")
        if assign_count:
            reserved = [c for c, a in zip(transfer_cards, assignments) if a is not None] + assign_only
            parts.append(f"{plural(assign_count, 'vorgemerkte Karte', 'vorgemerkte Karten')} auf diesem "
                         "ESPuino zuweisen:" + NL + names(reserved))
        notes = []
        if not can_assign and plans:
            notes.append("Zugewiesen wird noch nicht – der ESPuino ist nicht erreichbar. Danach: SD-Karte zurück, "
                         "ESPuino einschalten, verbinden, „Vorgemerkte markieren“ und „Ausgewählte übertragen“.")
        if waiting:
            notes.append(f"{plural(len(waiting), 'Karte ist', 'Karten sind')} komplett auf der SD-Karte und "
                         "bleiben vorgemerkt, bis der ESPuino verbunden ist.")
        if complete:
            notes.append(f"{plural(len(complete), 'Karte ist', 'Karten sind')} schon vollständig und "
                         "zugewiesen – nichts zu tun.")
        if impossible:
            notes.append(f"{plural(len(impossible), 'Karte geht', 'Karten gehen')} nicht: Ordner fehlt auf der "
                         "SD-Karte, und es gibt kein Album in der Bibliothek." + NL + names(impossible))
        if errors:
            notes.append("Nicht geprüft:" + NL + NL.join(errors))
        if not parts:
            QMessageBox.information(self, title, (NL * 2).join(notes) or "Nichts zu tun.")
            return
        answer = QMessageBox.question(self, title, (NL * 2).join(parts + notes + ["Fortfahren?"]))
        if answer != QMessageBox.StandardButton.Yes:
            return
        if not plans:
            self._espuino_assign_cards(assign_only)
            return

        def finished(cancelled):
            if assign_only and not cancelled:
                self._espuino_assign_cards(assign_only)
            else:
                self._mediahub_reload()

        self._espuino_run_transfer(plans, local, host, assignments, finished)

    def _espuino_assign_cards(self, cards: list):
        """Bekannte Karten ohne Uebertragen zuweisen (Ordner liegt schon auf der SD-Karte)"""
        _source, host, _sd_root = dev.settings()
        errors, done = [], 0
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            for card in cards:
                try:
                    dev.assign_card(host, card.card_id, card.sd_path, int(card.play_mode or 3))
                    done += 1
                except dev.DeviceError as exc:
                    errors.append(f"{card.label}: {exc}")
        finally:
            QApplication.restoreOverrideCursor()
        self._status(f"{done} Karte{'' if done == 1 else 'n'} zugewiesen")
        if errors:
            QMessageBox.warning(self, "Zuweisen", "\n".join(errors))
        self._mediahub_reload()

    def _espuino_transfer_card(self, card_id: str):
        card = self._mediahub_card(card_id)
        if card is not None:
            self._espuino_transfer_cards([card])

    def _espuino_normalize_wanted(self) -> bool:
        """Lautstaerke beim Uebertragen angleichen: gemerkte Wahl, nur mit FFmpeg"""
        from core.loudness_copy import normalize_on_transfer
        return self.audio_converter.is_available and normalize_on_transfer()

    def _espuino_run_transfer(self, plans, local: str, host: str, assignments, finished, normalize=None):
        """normalize: None = gemerkte Wahl aus dem Uebertragen-Dialog"""
        from core.loudness_copy import TransferNormalizer
        if normalize is None:
            normalize = self._espuino_normalize_wanted()
        normalizer = TransferNormalizer(self.audio_converter) if normalize else None
        self._release_files()
        progress = QProgressDialog("Übertrage …", "Abbrechen", 0, 1000, self)
        progress.setWindowTitle("Auf den ESPuino übertragen")
        progress.setWindowModality(Qt.WindowModality.WindowModal)
        progress.setMinimumDuration(0)
        progress.setAutoClose(False)
        progress.setAutoReset(False)
        worker = TransferWorker(plans, local, host, self, assignments=assignments, normalizer=normalizer)
        self._espuino_transfer_worker = worker
        progress.canceled.connect(worker.cancel)
        assigned = []

        def on_progress(done, total, text):
            progress.setValue(int(done * 1000 / total) if total else 0)
            progress.setLabelText(text + f"\n{done / 1e6:.0f} von {total / 1e6:.0f} MB")

        def on_failed(message):
            progress.close()
            QMessageBox.warning(self, "Auf den ESPuino übertragen", message)
            self._mediahub_reload()

        def on_done(copied, skipped, cancelled):
            progress.close()
            text = f"{copied} Datei{'' if copied == 1 else 'en'} übertragen"
            if skipped:
                text += f", {skipped} schon vorhanden"
            if assigned:
                text += f", {len(assigned)} Karte{'' if len(assigned) == 1 else 'n'} zugewiesen"
            if normalizer is not None and normalizer.summary():
                text += "; " + normalizer.summary()
            self._status(text + (" – abgebrochen" if cancelled else ""))
            if normalizer is not None and normalizer.errors:
                QMessageBox.warning(self, "Lautstärke angleichen",
                                    f"{len(normalizer.errors)} Datei(en) ließen sich nicht messen oder angleichen "
                                    "und wurden unverändert übertragen:\n\n" + "\n".join(normalizer.errors[:5]))
            finished(cancelled)

        worker.progress.connect(on_progress)
        worker.failed.connect(on_failed)
        worker.done.connect(on_done)
        worker.assigned.connect(lambda card_id, _folder, _mode: assigned.append(card_id))
        worker.start()

    # --- Lautstaerke angleichen auf der SD-Karte im Kartenleser ------------------------

    def _espuino_can_normalize(self) -> bool:
        """Fuers Rechtsklick-Menue: ohne Blick auf die SD-Karte (das kann bei einem leeren
        Kartenleser Sekunden dauern); ob sie wirklich steckt, prueft erst die Aktion"""
        _source, _host, sd_root = dev.settings()
        return bool(sd_root) and self.audio_converter.is_available

    @staticmethod
    def _espuino_folder_audio(sd_root: str, folders: list):
        """(MP3-Dateien, Zahl anderer Audiodateien) in den Ordnern, auch in Unterordnern"""
        from pathlib import Path
        from core.loudness_copy import is_mp3
        mp3, other, seen = [], 0, set()
        for folder in folders:
            local = Path(sd_root).joinpath(*[p for p in folder.split("/") if p])
            if not local.is_dir():
                continue
            for path in sorted(local.rglob("*")):
                if path.name.startswith(".") or not path.is_file() or path in seen:
                    continue  # versteckte Dateien, etwa ._ von macOS oder .tmp_norm_
                seen.add(path)
                if is_mp3(path.name):
                    mp3.append(path)
                elif path.suffix.lower() in dev.AUDIO_EXTENSIONS:
                    other += 1
        return mp3, other

    def _espuino_normalize_folders(self, folders: list):
        """Wie "Lautstaerke angleichen" im TonUINO-Modus, fuer Ordner auf der SD-Karte
        des ESPuino im Kartenleser: messen, dann die abweichenden Dateien ersetzen"""
        from types import SimpleNamespace
        title = "Lautstärke angleichen"
        if not folders:
            return
        if not self.audio_converter.is_available:
            QMessageBox.warning(self, "FFmpeg fehlt", "FFmpeg ist nicht verfügbar.")
            return
        _source, _host, sd_root = dev.settings()
        if not (sd_root and dev.sd_available(sd_root)):
            QMessageBox.information(self, title, "Das geht nur, wenn die SD-Karte des ESPuino im Kartenleser steckt. "
                                    "Beim Übertragen aus der Bibliothek lässt sich die Lautstärke auch über "
                                    "das WLAN angleichen (Haken „Lautstärke angleichen“).")
            return
        files, other = self._espuino_folder_audio(sd_root, folders)
        if not files:
            QMessageBox.information(self, title, "Keine MP3-Dateien in "
                                    + (f"„{folders[0]}“." if len(folders) == 1 else f"den {len(folders)} Ordnern."))
            return
        where = f"im Ordner „{folders[0]}“" if len(folders) == 1 else f"in {len(folders)} Ordnern"
        text = (f"Soll die Lautstärke der {len(files)} MP3-Datei(en) {where} auf {loudness.TARGET_LUFS:.0f} LUFS "
                "angeglichen werden?\n\nDateien, die bereits passen, bleiben unverändert. Alle anderen werden neu "
                "kodiert (leicht verlustbehaftet) und auf der SD-Karte ersetzt. Das lässt sich nicht rückgängig "
                "machen; die Dateien in der Bibliothek bleiben unverändert.")
        if other:
            text += f"\n\n{other} Datei(en) in anderen Formaten werden übersprungen."
        if not confirm_action(self, title, text):
            return
        root = os.path.normpath(sd_root)
        tracks = [SimpleNamespace(filepath=str(p), filename=os.path.relpath(str(p), root).replace("\\", "/"))
                  for p in files]
        self._espuino_loudness_rows, self._espuino_loudness_errors = [], []
        dialog = QProgressDialog("Lautstärke wird gemessen …", "Abbrechen", 0, len(tracks), self)
        dialog.setWindowTitle(title)
        dialog.setWindowModality(Qt.WindowModality.WindowModal)
        dialog.setMinimumDuration(0)
        dialog.setValue(0)
        self._espuino_loudness_dialog = dialog
        phase = ["Messen"]

        def on_progress(current, total, name):
            dialog.setValue(current - 1)
            dialog.setLabelText(f"{phase[0]}: {current}/{total} – {name}")

        def finish(done):
            close_progress_dialog(dialog)
            errors = self._espuino_loudness_errors
            unchanged = len(self._espuino_loudness_rows) - done - len(errors)
            if done == 0 and not errors:
                self._status("Alle Dateien liegen bereits im Zielbereich")
            else:
                self._status(f"Lautstärke von {done} Datei(en) angeglichen, {max(unchanged, 0)} passten schon")
            if errors:
                QMessageBox.warning(self, "Nicht alle Dateien angepasst",
                                    f"{len(errors)} Datei(en) konnten nicht angepasst werden (unverändert):\n\n"
                                    + "\n".join(errors[:5]))
            self._mediahub_reload()

        def analysis_finished():
            if analysis.was_cancelled:
                close_progress_dialog(dialog)
                self._status("Lautstärke-Angleichung abgebrochen")
                return
            changes = []
            for track, measured, error in self._espuino_loudness_rows:
                if error:
                    self._espuino_loudness_errors.append(f"{track.filename}: {error}")
                    continue
                gain = loudness.gain_for(loudness.LoudnessResult(integrated=measured)) if measured is not None else None
                if gain is not None:
                    changes.append((track, gain))
            if not changes:
                finish(0)
                return
            self._release_files()  # der Player koennte eine der Dateien geoeffnet haben
            dialog.setMaximum(len(changes))
            dialog.setValue(0)
            phase[0] = "Anpassen"
            apply = LoudnessApplyWorker(changes, self.audio_converter)
            apply.progress.connect(on_progress)
            apply.error.connect(lambda name, message: self._espuino_loudness_errors.append(f"{name}: {message}"))
            apply.completed.connect(finish)
            self._espuino_loudness_workers.append(apply)
            apply.start()

        analysis = LoudnessAnalysisWorker(tracks, self.audio_converter)
        analysis.progress.connect(on_progress)
        analysis.measured.connect(lambda track, lufs, error: self._espuino_loudness_rows.append((track, lufs, error)))
        analysis.completed.connect(analysis_finished)
        self._espuino_loudness_workers = [analysis]

        def cancel():
            for worker in self._espuino_loudness_workers:
                worker.cancel()

        keep_open_until_done(dialog, cancel, "Wird nach der aktuellen Datei abgebrochen …")
        analysis.start()

    def _mediahub_cover_map(self) -> dict:
        archive = get_archive(self, ask=False)
        result = {}
        for card in self._mediahub_data.cards if self._mediahub_data else []:
            result[card.card_id] = archive is not None and card_paths(str(archive.card_dir(card.album_id)))[0].is_file()
        return result

    def _mediahub_show_overview(self, has_cover: dict):
        data = self._mediahub_data
        if self._espuino_is_device() and data is not None:
            self._espuino_show_overview(has_cover)
            return
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

    def _espuino_show_overview(self, has_cover: dict):
        from gui.espuino_device_view import needs_transfer
        data, state = self._mediahub_data, self._espuino_state
        total = len(data.cards)
        without = sum(1 for c in data.cards if not has_cover.get(c.card_id))
        missing = sum(1 for c in data.cards if needs_transfer(c))
        lines = [f"{total} bekannte Karte{'' if total == 1 else 'n'}, davon {without} ohne Kartencover."]
        if state is not None and not state.reachable and state.backup_date:
            lines.append(f"Der ESPuino ist nicht erreichbar – „Zugewiesen“ zeigt den Stand seiner Sicherung "
                         f"backup.txt auf der SD-Karte vom {state.backup_date}.")
            if missing:
                lines.append(f"{missing} Karte{' lässt' if missing == 1 else 'n lassen'} sich auf die SD-Karte "
                             "übertragen; zugewiesen wird nach dem Verbinden.")
        elif state is not None and not state.reachable:
            lines.append(f"Der ESPuino ist nicht erreichbar – Status unbekannt ({state.error})")
        elif missing:
            lines.append(f"{missing} Karte{' lässt' if missing == 1 else 'n lassen'} sich auf diesen ESPuino "
                         "übertragen (Ordner fehlt auf der SD-Karte oder nicht zugewiesen, Album in der Bibliothek).")
        skipped = []
        if data.skipped_function:
            skipped.append(f"{data.skipped_function} Funktionskarte{'' if data.skipped_function == 1 else 'n'}")
        if data.skipped_webradio:
            skipped.append(f"{data.skipped_webradio} Webradio-Karte{'' if data.skipped_webradio == 1 else 'n'}")
        if data.skipped_mediahub:
            skipped.append(f"{data.skipped_mediahub} vom Mediahub verwaltet")
        if skipped:
            lines.append("Auf dem ESPuino außerdem: " + ", ".join(skipped) + ".")
        self.mediahub_page.show_device_overview(" ".join(lines), can_batch=without > 0, can_print=total > without)

    def _mediahub_card(self, card_id: str = ""):
        page = self.mediahub_page
        card_id = card_id or (page.card.card_id if page.card is not None else "") \
            or self.mediahub_sidebar.current_card_id()
        return mh.card_by_id(self._mediahub_data, card_id)

    def _mediahub_show_card(self, card_id: str):
        card = self._mediahub_card(card_id)
        if card is None:
            return
        folder = mh.build_folder(card, self._mediahub_media_root, get_archive(self, ask=False))
        self.mediahub_page.can_delete_files = self._espuino_is_device() and self._espuino_delete_target() is not None
        self.mediahub_page.show_card(card, folder, self._espuino_place())
        self._espuino_set_play_entries(self.mediahub_page.file_list, self._espuino_card_play_entries(card, folder))
        self.mediahub_page.set_series(self._mediahub_card_series(card))
        if self._espuino_is_device():  # Karte und Ordner in einer Ansicht
            state = self._espuino_state
            self.mediahub_page.set_folder_cards(self._espuino_folder_cards(card.sd_path), self._mediahub_cover_map(),
                                                state is not None and state.reachable)

    def _mediahub_on_card_clicked(self, card_id: str):
        self._mediahub_show_card(card_id)
        if self.view_switch.currentIndex() != self.tab_sd:
            self.view_switch.setCurrentIndex(self.tab_sd)

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
        album = self.library_view.index.find(card.library_path) if card.library_path else None
        if album is not None and card.library_album_id and album.album_id == card.library_album_id:
            # Karte gehoert zu einem Album der Bibliothek: dessen Kartencover gilt, keine eigene Kopie
            self._library_cover(album, focus=CoverManagerDialog.FOCUS_CARD)
            self._mediahub_reload()
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

    # --- Serienstapel und Nacharbeiten (Methoden aus gui/main_window_cards.py) --------

    def _series_batch_ready(self) -> bool:
        if not self._is_espuino():
            return super()._series_batch_ready()
        return self._mediahub_require_data()

    def _series_unit(self) -> str:
        return "Karten" if self._is_espuino() else super()._series_unit()

    def _series_folders(self, profile, archive) -> list:
        """ESPuino-Modus: Karten ohne Kartencover, die zur Serie gehoeren - ueber das
        Album im Archiv zugeordnet oder mit erkannter Reihe gleichen Namens (Tags)"""
        if not self._is_espuino():
            return super()._series_folders(profile, archive)
        from core.cover_research import gather_folder_info
        from core.series import detect_series, normalize_series_name
        key = normalize_series_name(profile.name)
        result, seen = [], set()
        for folder in self._mediahub_folders(archive):
            if not folder.tracks or folder.card_path or folder.album_id in seen:
                continue  # mehrere Karten fuer dasselbe Album: ein Cover reicht
            record = archive.load_album(folder.album_id) if folder.album_id else None
            if record is not None and record.series_id:
                if record.series_id == profile.id:
                    result.append(folder)
                    seen.add(folder.album_id)
                continue
            info = gather_folder_info(folder, self.metadata_manager, max_titles=5)
            name, _mb = detect_series(info, lookup=lambda _rg: [])
            if name and normalize_series_name(name) == key:
                result.append(folder)
                seen.add(folder.album_id)
        return result

    def _open_cover_for_batch_folder(self, folder):
        if isinstance(folder, mh.MediahubFolder):
            self._mediahub_open_cover(folder.card_id)
            return
        super()._open_cover_for_batch_folder(folder)

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
