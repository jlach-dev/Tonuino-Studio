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

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QAction, QActionGroup
from PyQt6.QtWidgets import QMenu, QMessageBox, QProgressDialog

from core import espuino_cards
from core import espuino_device as dev
from core.espuino_transfer import plan_album
from core import mediahub_cards as mh
from core.app_mode import MODE_ESPUINO, MODE_TONUINO, MODES, current_mode, display_name, set_mode
from core.card_store import card_paths
from gui.about_dialog import show_about
from gui.cover_manager_dialog import CoverManagerDialog, get_archive
from gui.espuino_dialogs import AssignCardDialog, EspuinoSetupDialog, TransferDialog, TransferWorker
from gui.espuino_device_view import EspuinoDeviceSidebar, album_folder_names
from gui.mediahub_view import MediahubPage, MediahubSidebar
from gui.print_sheet_dialog import PrintSheetDialog

log = logging.getLogger(__name__)

MODE_MENU_LABELS = {
    MODE_TONUINO: "TonUINO – SD-Karte und RFID-Karten",
    MODE_ESPUINO: "ESPuino – Karten vom Gerät oder aus dem Mediahub",
}


class MediahubMixin:
    """Methoden von MainWindow (siehe Modul-Doku)"""

    # main.py setzt das: beim Start nicht sofort im WLAN fragen (ein ausgeschalteter
    # ESPuino wuerde den Start aufhalten), sondern nach dem Anzeigen fragen, womit
    # verbunden werden soll (espuino_start_session). Tests verbinden sofort.
    espuino_ask_on_start = False

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
                       self.action_wipe_card, self.action_series_cover_batch, self.action_tag_lookup):
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
        self.espuino_sidebar = EspuinoDeviceSidebar()
        self.espuino_sidebar.setup_requested.connect(self._mediahub_setup)
        self.espuino_sidebar.connect_requested.connect(self.espuino_connect)
        self.espuino_sidebar.reload_requested.connect(self._mediahub_reload)
        self.espuino_sidebar.assign_requested.connect(self._espuino_assign)
        self.espuino_sidebar.read_requested.connect(self._espuino_read_cards)
        self.espuino_sidebar.assign_folder_requested.connect(self._espuino_assign_folder)
        self.espuino_sidebar.folder_selected.connect(self._espuino_show_folder)
        self.espuino_sidebar.albums_dropped.connect(self._espuino_transfer)
        self.espuino_sidebar.folder_list.album_names = lambda paths: album_folder_names(paths,
                                                                                         self.library_view.index)
        sidebar_layout.insertWidget(sidebar_layout.indexOf(self.folder_list), self.espuino_sidebar, 1)

        self.mediahub_page = MediahubPage()
        self.mediahub_page.setup_requested.connect(self._mediahub_setup)
        self.mediahub_page.cover_requested.connect(self._mediahub_open_cover)
        self.mediahub_page.batch_requested.connect(self._mediahub_generate_missing)
        self.mediahub_page.print_requested.connect(self._mediahub_print_sheet)
        self.mediahub_page.transfer_requested.connect(self._espuino_transfer_card)
        self.mediahub_page.back_requested.connect(self._espuino_back)
        self.mediahub_page.cards_table.card_selected.connect(self._mediahub_show_card)
        self.mediahub_page.folder_page.card_selected.connect(self._mediahub_show_card)
        self.mediahub_page.folder_page.assign_requested.connect(self._espuino_assign_folder)
        self.mediahub_page.cards_table.transfer_missing_requested.connect(self._espuino_transfer_missing)
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
                self._espuino_connected = True  # der Dialog hat die Verbindung geprueft
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
        self.espuino_sidebar.set_state(host, state, folder_cards, device.label if device else "")
        has_cover = self._mediahub_cover_map()
        page.cards_table.set_cards(data.cards, has_cover, state.reachable)
        if page.card is not None and mh.card_by_id(data, page.card.card_id) is not None:
            self._mediahub_show_card(page.card.card_id)
        else:
            self._mediahub_show_overview(has_cover)
        offline = "" if state.reachable else " – ohne ESPuino" if not self._espuino_connected else             " – ESPuino nicht erreichbar"
        self._status(f"{len(data.cards)} bekannte Karten" + offline)

    def _espuino_back(self):
        self.mediahub_page.card = None
        self._mediahub_show_overview(self._mediahub_cover_map())

    def _espuino_album_for_folder(self, sd_folder: str):
        return espuino_cards.find_library_album(espuino_cards.KnownCard("", sd_folder=sd_folder),
                                                self.library_view.index)

    def _espuino_assign(self, preset_card: str = "", preset_path: str = "", album=None):
        if not self._espuino_is_device() or not self._espuino_host:
            return
        _source, host, sd_root = dev.settings()
        known = getattr(self, "_espuino_known", None) or espuino_cards.load(self._espuino_data_dir())
        labels = {cid: card.label for cid, card in known.cards.items()}
        dialog = AssignCardDialog(host, sd_root, labels, self)
        if preset_card:
            dialog.card_edit.setText(preset_card)
        if preset_path:
            dialog.path_edit.setText(preset_path)
        accepted = dialog.exec()
        entry = dialog.result_entry
        play_mode = dialog.play_mode
        dialog.deleteLater()
        if not accepted or not entry:
            return
        card_id, folder = str(entry.get("id")), str(entry.get("fileOrUrl") or "")
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
        self._status(f"Karte {mh.format_card_id(card_id)} zugewiesen: {folder}")
        self._mediahub_reload()
        if mh.card_by_id(self._mediahub_data, card_id) is not None:
            self._mediahub_on_card_clicked(card_id)

    # --- Karte auslesen ---------------------------------------------------------------

    def _espuino_read_cards(self):
        """Fenster, das jede aufgelegte Karte zeigt (bleibt offen, nicht modal)"""
        from gui.espuino_card_reader import CardReaderDialog
        reader = getattr(self, "_espuino_reader", None)
        if reader is not None:
            reader.raise_()
            reader.activateWindow()
            return
        if not self._espuino_is_device() or not self._espuino_host:
            return
        reader = CardReaderDialog(self._espuino_host, self._espuino_card_info, self)
        reader.show_card_requested.connect(self._espuino_reader_show_card)
        reader.assign_requested.connect(lambda card_id: self._espuino_assign(preset_card=card_id))
        reader.destroyed.connect(lambda *_: setattr(self, "_espuino_reader", None))
        self._espuino_reader = reader
        reader.show()

    def _espuino_reader_show_card(self, card_id: str):
        if mh.card_by_id(self._mediahub_data, card_id) is not None:
            self.view_switch.setCurrentIndex(0)
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
            info.play_mode = mh.play_mode_label(str(entry[1]))
        card = mh.card_by_id(self._mediahub_data, card_id)
        if card is None:
            if entry is not None:
                info.status = "Seit dem letzten Einlesen neu zugewiesen – „Neu einlesen“ zeigt mehr."
            return info
        info.known, info.name = True, card.label
        if not info.play_mode and card.play_mode:
            info.play_mode = mh.play_mode_label(card.play_mode)
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

    def _espuino_show_folder(self, folder: str):
        """Ordner der SD-Karte: Album aus der Bibliothek und alle Karten, die darauf zeigen"""
        data = self._mediahub_data
        if not folder or data is None:
            return
        from gui.espuino_device_view import in_folder
        cards = [c for c in data.cards if in_folder(c.sd_path, folder) or in_folder(c.device_target, folder)]
        meta = []
        album = next((self.library_view.index.find(c.library_path) for c in cards if c.library_path), None)             or self._espuino_album_for_folder(folder)
        preview = ""
        if album is not None:
            minutes = int(round(album.duration / 60)) if album.duration else 0
            meta.append(f"Album: {album.name}" + (f" – {album.artist}" if album.artist else ""))
            meta.append(f"{album.track_count} Titel" + (f", {minutes} min" if minutes else "")
                        + f" · Bibliothek: {album.path}")
            preview = self._espuino_album_thumb(album)
        else:
            meta.append("Kein passendes Album in der Bibliothek gefunden.")
        has_cover = self._mediahub_cover_map()
        archive = get_archive(self, ask=False)
        if archive is not None:  # lieber das Kartencover zeigen als das Album-Cover
            for card in cards:
                front = card_paths(str(archive.card_dir(card.album_id)))[0]
                if front.is_file():
                    preview = str(front)
                    break
        state = self._espuino_state
        reachable = state is not None and state.reachable
        if len(cards) > 1:
            meta.append(f"{len(cards)} Karten zeigen auf diesen Ordner – jede spielt dasselbe Album.")
        self.mediahub_page.show_folder(folder, meta, preview, cards, has_cover, reachable)
        if self.view_switch.currentIndex() != 0:
            self.view_switch.setCurrentIndex(0)

    def _espuino_assign_folder(self, folder: str):
        self._espuino_assign(preset_path=folder, album=self._espuino_album_for_folder(folder))

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
        dialog = TransferDialog(albums, sd_root, host, self)
        if not dialog.exec():
            dialog.deleteLater()
            return
        plans, assign_after, local = dialog.plans, dialog.assign_after, dialog.sd_root
        dialog.deleteLater()

        def finished(cancelled):
            if not cancelled and assign_after and len(plans) == 1:
                self._espuino_assign(preset_path=plans[0].folder, album=albums[0])
            else:
                self._mediahub_reload()

        self._espuino_run_transfer(plans, local, host, None, finished)

    def _espuino_transfer_cards(self, cards: list):
        """Bekannte Karten auf diesen ESPuino: Album aus der Bibliothek in den
        bekannten Ordner (fehlende Dateien), danach die Karte zuweisen"""
        from gui.espuino_device_view import can_transfer
        if self._espuino_transfer_busy():
            return
        cards = [c for c in cards if can_transfer(c)]
        if not cards:
            return
        _source, host, sd_root = dev.settings()
        local = sd_root if sd_root and os.path.isdir(sd_root) else ""
        plans, assignments = [], []
        for card in cards:
            album = self.library_view.index.find(card.library_path)
            if album is None or not album.tracks:
                continue
            plans.append(plan_album(album.tracks, card.sd_path))
            assignments.append((card.card_id, int(card.play_mode or 3)))
        if not plans:
            return
        size = sum(p.total for p in plans)
        way = "auf die SD-Karte im Kartenleser" if local else "über das WLAN"
        names = "\n".join(f"• {c.label}" for c in cards[:12]) + ("\n• …" if len(cards) > 12 else "")
        answer = QMessageBox.question(
            self, "Auf den ESPuino übertragen",
            f"{len(plans)} Karte{'' if len(plans) == 1 else 'n'} {way} übertragen ({size / 1e6:.0f} MB, "
            f"vorhandene Dateien werden übersprungen) und auf diesem ESPuino zuweisen?\n\n{names}")
        if answer != QMessageBox.StandardButton.Yes:
            return
        self._espuino_run_transfer(plans, local, host, assignments, lambda _cancelled: self._mediahub_reload())

    def _espuino_transfer_card(self, card_id: str):
        card = self._mediahub_card(card_id)
        if card is not None:
            self._espuino_transfer_cards([card])

    def _espuino_transfer_missing(self):
        from gui.espuino_device_view import needs_transfer
        data = self._mediahub_data
        self._espuino_transfer_cards([c for c in (data.cards if data else []) if needs_transfer(c)])

    def _espuino_run_transfer(self, plans, local: str, host: str, assignments, finished):
        self._release_files()
        progress = QProgressDialog("Übertrage …", "Abbrechen", 0, 1000, self)
        progress.setWindowTitle("Auf den ESPuino übertragen")
        progress.setWindowModality(Qt.WindowModality.WindowModal)
        progress.setMinimumDuration(0)
        progress.setAutoClose(False)
        progress.setAutoReset(False)
        worker = TransferWorker(plans, local, host, self, assignments=assignments)
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
            self._status(text + (" – abgebrochen" if cancelled else ""))
            finished(cancelled)

        worker.progress.connect(on_progress)
        worker.failed.connect(on_failed)
        worker.done.connect(on_done)
        worker.assigned.connect(lambda card_id, _folder, _mode: assigned.append(card_id))
        worker.start()

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
        if state is not None and not state.reachable:
            lines.append(f"Der ESPuino ist nicht erreichbar – Status unbekannt ({state.error})")
        elif missing:
            lines.append(f"{missing} Karte{'' if missing == 1 else 'n'} lassen sich auf diesen ESPuino übertragen "
                         "(Ordner fehlt oder nicht zugewiesen).")
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
