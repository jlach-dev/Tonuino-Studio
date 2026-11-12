"""
ESPuino direkt (siehe core/espuino_device.py, core/espuino_cards.py):

- EspuinoDeviceSidebar: Seitenleiste mit dem Stand des Geraets und den Ordnern
  auf seiner SD-Karte. Dorthin zieht man Alben aus der Bibliothek.
- KnownCardsTable: die bekannten Karten im Reiter "ESPuino-Karten" mit Status
  (zugewiesen auf diesem Geraet, Ordner auf der SD-Karte, Kartencover).

Eigene Datei des Forks (siehe core/branding.py).
"""

import html
from typing import Dict, List, Optional

from PyQt6.QtCore import QPoint, Qt, pyqtSignal
from PyQt6.QtGui import QBrush, QColor, QKeySequence, QPixmap, QShortcut
from PyQt6.QtWidgets import (
    QAbstractItemView, QComboBox, QFrame, QHBoxLayout, QHeaderView, QLabel, QLineEdit, QListWidget, QListWidgetItem, QMenu, QPushButton,
    QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget,
)

from core.espuino_device import NOT_CONNECTED, same_folder
from core.espuino_cards import ASSIGNED, ASSIGNED_OTHER, NOT_ASSIGNED
from core.espuino_transfer import safe_name
from core.mediahub_cards import MediahubCard, format_card_id
from gui import theme
from gui.library_dnd import DropHighlight, drop_payload_from_mime

FOLDER_ROLE = Qt.ItemDataRole.UserRole
FOLDER_PREVIEW = 200
SERIES_ROLE = Qt.ItemDataRole.UserRole + 1


def _gb(value: int) -> str:
    return f"{value / 1e9:.1f}".replace(".", ",") + " GB"


class DeviceFolderList(QListWidget):
    """Ordner auf der SD-Karte des ESPuino als Drop-Ziel fuer Alben der Bibliothek.
    Freie Flaeche: neuer Ordner. Ordner mit dem Namen des Albums: fehlende Dateien
    ergaenzen. Ein anderer Ordner nimmt nichts an."""
    albums_dropped = pyqtSignal(list)  # Albumpfade

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setAcceptDrops(True)
        self.drop_enabled = False
        self.drop_hint_blocked = ""  # Grund, wenn drop_enabled False
        self.drop_highlight = DropHighlight(self.viewport())
        self.album_names = lambda paths: []  # setzt das Hauptfenster: Albumpfade -> Ordnernamen

    def _check(self, event):
        """(annehmen, Rechteck, Hinweis, gesperrt)"""
        payload = drop_payload_from_mime(event.mimeData())
        if not payload.albums:
            return False, None, "", False
        if not self.drop_enabled:
            return False, self.viewport().rect(), self.drop_hint_blocked or "ESPuino nicht eingerichtet", True
        item = self.itemAt(event.position().toPoint())
        count = len(payload.albums)
        if item is None:
            return True, self.viewport().rect(), "Neuer Ordner" if count == 1 else f"{count} neue Ordner", False
        folder = str(item.data(FOLDER_ROLE) or "").strip("/").casefold()
        names = [n.casefold() for n in self.album_names(payload.albums)]
        if count == 1 and names and names[0] == folder:
            return True, self.visualItemRect(item), "Fehlende Dateien ergänzen", False
        return False, self.visualItemRect(item), "Anderer Ordner – auf freie Fläche ziehen", True

    def dragEnterEvent(self, event):
        if drop_payload_from_mime(event.mimeData()).albums:
            event.accept()  # sonst kommen keine dragMove-Ereignisse und kein Hinweis
            self.dragMoveEvent(event)
            return
        event.ignore()

    def dragMoveEvent(self, event):
        ok, rect, hint, blocked = self._check(event)
        if rect is None:
            event.ignore()
            self.drop_highlight.hide()
            return
        self.drop_highlight.show_at(rect, hint, blocked=blocked)
        if ok:
            event.setDropAction(Qt.DropAction.CopyAction)
            event.accept()
        else:
            event.ignore()

    def dragLeaveEvent(self, event):
        self.drop_highlight.hide()
        event.accept()

    def dropEvent(self, event):
        self.drop_highlight.hide()
        ok, _rect, _hint, _blocked = self._check(event)
        if not ok:
            event.ignore()
            return
        event.setDropAction(Qt.DropAction.CopyAction)
        event.accept()
        self.albums_dropped.emit(drop_payload_from_mime(event.mimeData()).albums)


class EspuinoDeviceSidebar(QWidget):
    """Seitenleiste im ESPuino-Modus mit Quelle "ESPuino direkt"""
    setup_requested = pyqtSignal()
    connect_requested = pyqtSignal()
    assign_requested = pyqtSignal()
    reload_requested = pyqtSignal()
    albums_dropped = pyqtSignal(list)
    assign_folder_requested = pyqtSignal(str)  # "/Ordner"
    folder_selected = pyqtSignal(str)  # "/Ordner": Infos und Karten zeigen
    delete_folders_requested = pyqtSignal(list)  # "/Ordner", ...

    def __init__(self, parent=None):
        super().__init__(parent)
        self.can_delete = False  # ESPuino erreichbar oder seine SD-Karte im PC
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)
        self.device_label = QLabel()
        self.device_label.setWordWrap(True)
        self.device_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(self.device_label)
        self.btn_connect = QPushButton("Verbinden …")
        self.btn_connect.setToolTip("Mit einem ESPuino im WLAN verbinden oder zu einem anderen wechseln")
        self.btn_connect.clicked.connect(self.connect_requested)
        layout.addWidget(self.btn_connect)
        self.btn_setup = QPushButton("ESPuino einrichten …")
        self.btn_setup.setToolTip("Adresse des ESPuino und SD-Karte im Kartenleser, oder den Mediahub wählen")
        self.btn_setup.clicked.connect(self.setup_requested)
        layout.addWidget(self.btn_setup)
        self.btn_assign = QPushButton("Karten auslesen und zuweisen …")
        self.btn_assign.setObjectName("primaryButton")
        self.btn_assign.setToolTip("Karte an den ESPuino halten oder auf den ACR122U legen: sehen, welche es ist "
                                   "(mit Kartencover), und einem Ordner auf dem ESPuino zuweisen")
        self.btn_assign.clicked.connect(self.assign_requested)
        layout.addWidget(self.btn_assign)
        self.btn_reload = QPushButton("Neu einlesen")
        self.btn_reload.setToolTip("Zuweisungen und Ordner erneut vom ESPuino lesen")
        self.btn_reload.clicked.connect(self.reload_requested)
        layout.addWidget(self.btn_reload)
        self.folders_label = QLabel("Auf dem ESPuino:")
        layout.addWidget(self.folders_label)
        self.series_combo = QComboBox()
        self.series_combo.setToolTip("Nur Ordner einer Serie zeigen (Serienprofil im Archiv, sonst Interpret "
                                     "des Albums in der Bibliothek)")
        self.series_combo.currentIndexChanged.connect(self._apply_folder_filter)
        layout.addWidget(self.series_combo)
        self.folder_filter = QLineEdit()
        self.folder_filter.setPlaceholderText("Ordner filtern …")
        self.folder_filter.setClearButtonEnabled(True)
        self.folder_filter.textChanged.connect(self._apply_folder_filter)
        layout.addWidget(self.folder_filter)
        self.folder_list = DeviceFolderList()
        self.folder_list.setObjectName("folderList")
        self.folder_list.albums_dropped.connect(self.albums_dropped)
        self.folder_list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.folder_list.customContextMenuRequested.connect(self._folder_menu)
        self.folder_list.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        delete_key = QShortcut(QKeySequence(QKeySequence.StandardKey.Delete), self.folder_list)
        delete_key.setContext(Qt.ShortcutContext.WidgetShortcut)
        delete_key.activated.connect(self._delete_selected)
        self.folder_list.itemClicked.connect(lambda item: self.folder_selected.emit(item.data(FOLDER_ROLE)))
        layout.addWidget(self.folder_list, 1)
        self.drop_label = QLabel("Alben aus der Bibliothek hierher ziehen.")
        self.drop_label.setWordWrap(True)
        self.drop_label.setStyleSheet(f"color: {theme.OVERLAY2};")
        layout.addWidget(self.drop_label)
        self._fill_series([])  # Serienauswahl erst zeigen, wenn es Serien gibt

    def set_state(self, host: str, state, folder_cards: Dict[str, List[str]], name: str = "",
                  folder_series: Optional[Dict[str, str]] = None):
        """state: core.espuino_device.DeviceState oder None (nicht eingerichtet).
        folder_cards: "/Ordner" (klein) -> Kartennamen, die darauf zeigen.
        name: Name des Geraets in der Liste der ESPuinos.
        folder_series: "/Ordner" (klein) -> Serie (zum Anzeigen und Filtern)"""
        folder_series = folder_series or {}
        configured = bool(host)
        connected = state is not None and state.error != NOT_CONNECTED
        self.btn_assign.setEnabled(state is not None)  # auch ohne ESPuino: auslesen ueber den ACR122U
        self.can_delete = state is not None and bool(state.reachable or state.folders_from_pc)
        self.btn_reload.setEnabled(configured and connected)
        self.btn_connect.setText("ESPuino wechseln …" if configured and connected else "Verbinden …")
        self.btn_connect.setObjectName("" if connected else "primaryButton")
        self.btn_connect.style().unpolish(self.btn_connect)  # Stil nach dem Objektnamen neu anwenden
        self.btn_connect.style().polish(self.btn_connect)
        self.folder_list.clear()
        if state is None:
            self.device_label.setText("<b>ESPuino</b><br>noch nicht eingerichtet")
            self.folder_list.drop_enabled = False
            self.folder_list.drop_hint_blocked = "Zuerst „ESPuino einrichten“"
            self.folders_label.setText("Auf dem ESPuino:")
            return
        if not configured:
            self.device_label.setText("<b>Kein ESPuino verbunden</b><br>Bekannte Karten und Cover gehen trotzdem.")
            self.folder_list.drop_enabled = False
            self.folder_list.drop_hint_blocked = "Zuerst mit einem ESPuino verbinden"
            self.folders_label.setText("Auf dem ESPuino: (nicht verbunden)")
            return
        title = html.escape(name or f"ESPuino {host}")
        address = [host] if name and name != host else []
        if state.ip and state.ip != host:
            address.append(state.ip)
        lines = [f"<b>{title}</b>"] + ([html.escape(" · ".join(address))] if address else [])
        if not connected:
            lines.append(f"<span style='color:{theme.OVERLAY2}'>nicht verbunden</span>")
        elif state.reachable:
            lines.append(f"Firmware {state.firmware}")
            if state.total_bytes:
                lines.append(f"SD-Karte: {_gb(state.total_bytes)}, {_gb(state.free_bytes)} frei")
        else:
            lines.append(f"<span style='color:{theme.YELLOW}'>nicht erreichbar</span>")
        if state.folders_from_pc:
            lines.append("SD-Karte steckt im PC")
        if getattr(state, "sd_error", ""):
            lines.append(f"<span style='color:{theme.YELLOW}'>{html.escape(state.sd_error)}</span>")
        if state.backup_date:
            lines.append(f"Zuweisungen aus backup.txt vom {state.backup_date}")
        self.device_label.setText("<br>".join(lines))
        can_drop = state.reachable or state.folders_from_pc
        self.folder_list.drop_enabled = can_drop
        self.folder_list.drop_hint_blocked = "ESPuino nicht erreichbar" if connected else "Erst verbinden"
        for folder in state.folders or []:
            cards = folder_cards.get(folder.strip("/").casefold(), [])
            series = folder_series.get(folder.casefold(), "")
            item = QListWidgetItem(folder.strip("/") + (f"   ·   {series}" if series else ""))
            item.setData(FOLDER_ROLE, folder)
            item.setData(SERIES_ROLE, series)
            tips = [f"Serie: {series}"] if series else []
            if cards:
                tips.append("Karte: " + ", ".join(cards))
            else:
                item.setForeground(QBrush(QColor(theme.OVERLAY2)))
                tips.append("Keine Karte zeigt auf diesen Ordner")
            item.setToolTip("\n".join(tips))
            self.folder_list.addItem(item)
        self._fill_series(sorted({s for s in folder_series.values() if s}, key=str.casefold))
        self._apply_folder_filter()
        if state.folders is None:
            self.folders_label.setText("Auf dem ESPuino: (unbekannt)")
        else:
            self.folders_label.setText(f"Auf dem ESPuino: {len(state.folders)} Ordner")

    def _fill_series(self, names: List[str]):
        current = self.series_combo.currentData()
        self.series_combo.blockSignals(True)
        self.series_combo.clear()
        self.series_combo.addItem("Alle Serien", None)
        for name in names:
            self.series_combo.addItem(name, name)
        self.series_combo.addItem("Ohne Serie", "")
        index = self.series_combo.findData(current) if current is not None else 0
        self.series_combo.setCurrentIndex(max(0, index))
        self.series_combo.setVisible(bool(names))
        self.series_combo.blockSignals(False)
        self._apply_folder_filter()

    def _apply_folder_filter(self, *_):
        """Ordner nach Serie und nach Namen oder Kartennamen filtern (stehen im Tooltip)"""
        needle = self.folder_filter.text().strip().casefold()
        series = self.series_combo.currentData()
        for row in range(self.folder_list.count()):
            item = self.folder_list.item(row)
            text = (item.text() + " " + (item.toolTip() or "")).casefold()
            hidden = bool(needle) and needle not in text
            if series is not None and (item.data(SERIES_ROLE) or "") != series:
                hidden = True
            item.setHidden(hidden)

    def selected_folders(self) -> List[str]:
        return [i.data(FOLDER_ROLE) for i in self.folder_list.selectedItems() if not i.isHidden()]

    def _delete_selected(self):
        folders = self.selected_folders()
        if folders and self.can_delete:
            self.delete_folders_requested.emit(folders)

    def _folder_menu(self, pos: QPoint):
        item = self.folder_list.itemAt(pos)
        if item is None:
            return
        if not item.isSelected():
            self.folder_list.clearSelection()
            item.setSelected(True)
        folders = self.selected_folders()
        menu = QMenu(self)
        if len(folders) == 1 and self.btn_assign.isEnabled():
            action = menu.addAction("Karte zuweisen …")
            action.triggered.connect(lambda: self.assign_folder_requested.emit(item.data(FOLDER_ROLE)))
            menu.addSeparator()
        text = "Ordner löschen …" if len(folders) == 1 else f"{len(folders)} Ordner löschen …"
        delete = menu.addAction(text + "\tEntf")
        delete.setEnabled(self.can_delete)
        if not self.can_delete:
            delete.setToolTip("Nur mit verbundenem ESPuino oder seiner SD-Karte im PC")
        delete.triggered.connect(lambda: self.delete_folders_requested.emit(folders))
        menu.exec(self.folder_list.viewport().mapToGlobal(pos))


STATUS_TEXT = {ASSIGNED: "✓", NOT_ASSIGNED: "–", ASSIGNED_OTHER: "anders", "": "?"}


def can_transfer(card: MediahubCard) -> bool:
    """Karte laesst sich (erneut) auf das Geraet bringen: Album in der Bibliothek, Ordnerziel"""
    from core.espuino_device import FILE_MODES
    try:
        single = int(card.play_mode or 0) in FILE_MODES
    except ValueError:
        single = False
    return bool(card.library_path and card.sd_path) and not single


def can_assign_only(card: MediahubCard) -> bool:
    """Ohne Album in der Bibliothek, aber der Ordner liegt schon auf der SD-Karte:
    die Karte laesst sich (etwa nach dem Vormerken) zuweisen, nur nicht uebertragen"""
    return not can_transfer(card) and bool(card.sd_path) and card.folder_present is True


def needs_transfer(card: MediahubCard) -> bool:
    """Auf dem Geraet unvollstaendig: Ordner fehlt oder Karte nicht (so) zugewiesen"""
    return (can_transfer(card) or can_assign_only(card)) and (
        card.folder_present is False or card.device_status in (NOT_ASSIGNED, ASSIGNED_OTHER))


def is_reserved(card: MediahubCard) -> bool:
    """Vorgemerkt: im Kartenverzeichnis der App mit Ordner, auf diesem ESPuino
    aber (noch) nicht so zugewiesen - wartet auf „Ausgewaehlte uebertragen“"""
    return bool(card.sd_path) and card.device_status in (NOT_ASSIGNED, ASSIGNED_OTHER)


COL_CHECK, COL_NAME, COL_NUMBER, COL_ASSIGNED, COL_RESERVED, COL_SD, COL_LIBRARY, COL_COVER = range(8)
CARD_HEADERS = ["", "Karte", "Nummer", "Zugewiesen", "Vorgemerkt", "SD-Karte", "Bibliothek", "Cover"]
FILTER_MARK = " ▾"  # am Spaltenkopf: hier ist gefiltert


class KnownCardsTable(QWidget):
    """Bekannte Karten mit Status auf dem aktuellen ESPuino. Checkboxen fuer eine
    Auswahl ("Ausgewaehlte uebertragen"); ein Klick auf einen Spaltenkopf sortiert
    und filtert nach den Werten der Spalte."""
    card_selected = pyqtSignal(str)
    transfer_selected_requested = pyqtSignal(list)  # Kartennummern
    remove_requested = pyqtSignal(list)  # Kartennummern: Zuweisung entfernen und aus der App loeschen

    def __init__(self, parent=None):
        super().__init__(parent)
        self._checked = set()  # Kartennummern, bleibt beim Neueinlesen erhalten
        self._missing = set()  # Karten, die sich uebertragen lassen und fehlen
        self._reserved = set()  # vorgemerkt, auf diesem ESPuino noch nicht zugewiesen
        self._hidden_values = {}  # Spalte -> ausgeblendete Werte
        self._reachable = False
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.filter_edit = QLineEdit()
        self.filter_edit.setPlaceholderText("Karten filtern …")
        self.filter_edit.setClearButtonEnabled(True)
        self.filter_edit.textChanged.connect(self._apply_filter)
        layout.addWidget(self.filter_edit)
        self.tree = QTreeWidget()
        self.tree.setObjectName("trackList")
        self.tree.setRootIsDecorated(False)
        self.tree.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.tree.setHeaderLabels(CARD_HEADERS)
        header = self.tree.header()
        header.setSectionResizeMode(COL_CHECK, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(COL_NAME, QHeaderView.ResizeMode.Stretch)
        for column in range(COL_NUMBER, COL_COVER + 1):
            header.setSectionResizeMode(column, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionsClickable(True)
        header.setSortIndicatorShown(False)
        header.sectionClicked.connect(self._header_menu)
        header.setToolTip("Klick auf einen Spaltenkopf: sortieren und nach Werten filtern")
        self.tree.itemActivated.connect(self._open)
        self.tree.itemClicked.connect(self._open)
        self.tree.itemChanged.connect(self._on_item_changed)
        self.tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.tree.customContextMenuRequested.connect(self._card_menu)
        delete_key = QShortcut(QKeySequence(QKeySequence.StandardKey.Delete), self.tree)
        delete_key.setContext(Qt.ShortcutContext.WidgetShortcut)
        delete_key.activated.connect(lambda: self._remove(self._target_ids(self.tree.currentItem())))
        layout.addWidget(self.tree, 1)
        self.legend = QLabel("Zugewiesen: ✓ auf diesem ESPuino · – nicht zugewiesen · anders: dort anders belegt. "
                             "Vorgemerkt: ✓ in der App einem Ordner zugeordnet, auf dem ESPuino noch nicht – "
                             "wird mit „Ausgewählte übertragen“ zugewiesen. "
                             "SD-Karte: Ordner auf der SD-Karte des ESPuino ✓ oder fehlt. "
                             "Bibliothek: Album in der Bibliothek ✓ oder fehlt (nur dann lässt es sich übertragen). "
                             "Klick auf einen Spaltenkopf: sortieren und filtern.")
        self.legend.setWordWrap(True)
        self.legend.setStyleSheet(f"color: {theme.OVERLAY2};")
        layout.addWidget(self.legend)
        select_row = QHBoxLayout()
        for text, slot in (("Alle", lambda: self._check_where(lambda _cid: True)),
                           ("Keine", lambda: self._check_where(lambda _cid: False)),
                           ("Fehlende markieren", lambda: self._check_where(lambda cid: cid in self._missing)),
                           ("Vorgemerkte markieren", lambda: self._check_where(lambda cid: cid in self._reserved))):
            button = QPushButton(text)
            button.setObjectName("ghostButton")
            button.clicked.connect(slot)
            select_row.addWidget(button)
        select_row.addStretch(1)
        self.btn_transfer_selected = QPushButton("Ausgewählte übertragen")
        self.btn_transfer_selected.setObjectName("primaryButton")
        self.btn_transfer_selected.setToolTip("Nur die angehakten Karten: fehlende Dateien des Albums aus der "
                                              "Bibliothek übertragen und vorgemerkte Karten auf diesem ESPuino "
                                              "zuweisen. Vorher kommt eine Rückfrage mit der echten Datenmenge.")
        self.btn_transfer_selected.clicked.connect(
            lambda: self.transfer_selected_requested.emit(self.checked_ids()))
        select_row.addWidget(self.btn_transfer_selected)
        layout.addLayout(select_row)

    # --- Inhalt -----------------------------------------------------------------------

    def set_cards(self, cards: List[MediahubCard], has_cover: Dict[str, bool], reachable: bool):
        self._reachable = reachable
        self._missing = {c.card_id for c in cards if needs_transfer(c)}
        self._reserved = {c.card_id for c in cards if is_reserved(c)}
        known = {c.card_id for c in cards}
        self._checked &= known
        self.tree.blockSignals(True)
        self.tree.clear()
        for card in cards:
            folder = {True: "✓", False: "fehlt", None: "?"}[card.folder_present]
            status = STATUS_TEXT.get(card.device_status, "?")
            library = "✓" if card.library_path else "fehlt"
            reserved = "?" if not card.device_status else ("✓" if is_reserved(card) else "–")
            item = QTreeWidgetItem(["", card.label, format_card_id(card.card_id), status, reserved, folder, library,
                                    "✓" if has_cover.get(card.card_id) else "–"])
            item.setData(0, FOLDER_ROLE, card.card_id)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(COL_CHECK, Qt.CheckState.Checked if card.card_id in self._checked
                               else Qt.CheckState.Unchecked)
            tips = [f"Ordner: {card.sd_path or '–'}"]
            if card.device_status == ASSIGNED_OTHER:
                tips.append(f"Auf diesem ESPuino zeigt die Karte auf {card.device_target}")
            tips.append("Album in der Bibliothek: " + (card.library_path or "nicht gefunden"))
            item.setToolTip(COL_NAME, "\n".join(tips))
            warn = card.folder_present is False or card.device_status in (NOT_ASSIGNED, ASSIGNED_OTHER)
            if warn:
                for column in (COL_ASSIGNED, COL_RESERVED, COL_SD):
                    item.setForeground(column, QBrush(QColor(theme.YELLOW)))
            if not card.library_path:
                item.setForeground(COL_LIBRARY, QBrush(QColor(theme.OVERLAY2)))
            if not has_cover.get(card.card_id):
                font = item.font(COL_NAME)
                font.setItalic(True)
                item.setFont(COL_NAME, font)
            self.tree.addTopLevelItem(item)
        self.tree.blockSignals(False)
        self._apply_filter()
        self._update_selected_button()

    def _open(self, item, column: int):
        if column != COL_CHECK:  # Klick auf die Checkbox waehlt nur aus
            self.card_selected.emit(item.data(0, FOLDER_ROLE))

    def _on_item_changed(self, item, column: int):
        if column != COL_CHECK:
            return
        card_id = item.data(0, FOLDER_ROLE)
        if item.checkState(COL_CHECK) == Qt.CheckState.Checked:
            self._checked.add(card_id)
        else:
            self._checked.discard(card_id)
        self._update_selected_button()

    def _check_where(self, predicate):
        """Angezeigte Karten an- bzw. abhaken (ausgefilterte bleiben, wie sie sind)"""
        self.tree.blockSignals(True)
        for row in range(self.tree.topLevelItemCount()):
            item = self.tree.topLevelItem(row)
            if item.isHidden():
                continue
            card_id = item.data(0, FOLDER_ROLE)
            on = predicate(card_id)
            item.setCheckState(COL_CHECK, Qt.CheckState.Checked if on else Qt.CheckState.Unchecked)
            (self._checked.add if on else self._checked.discard)(card_id)
        self.tree.blockSignals(False)
        self._update_selected_button()

    def _target_ids(self, item) -> List[str]:
        """Angehakte Karten, wenn die Zeile dazugehoert, sonst nur diese Zeile"""
        if item is None:
            return []
        card_id = item.data(0, FOLDER_ROLE)
        checked = self.checked_ids()
        return checked if card_id in checked else [card_id]

    def _remove(self, ids: List[str]):
        if ids:
            self.remove_requested.emit(ids)

    def _card_menu(self, pos: QPoint):
        ids = self._target_ids(self.tree.itemAt(pos))
        if not ids:
            return
        menu = QMenu(self)
        text = "Karte entfernen …" if len(ids) == 1 else f"{len(ids)} Karten entfernen …"
        action = menu.addAction(text + "\tEntf")
        action.triggered.connect(lambda: self._remove(ids))
        menu.exec(self.tree.viewport().mapToGlobal(pos))

    def checked_ids(self) -> List[str]:
        return [self.tree.topLevelItem(row).data(0, FOLDER_ROLE) for row in range(self.tree.topLevelItemCount())
                if self.tree.topLevelItem(row).data(0, FOLDER_ROLE) in self._checked]

    def _update_selected_button(self):
        count = len(self._checked)
        self.btn_transfer_selected.setText(f"Ausgewählte übertragen ({count})")
        self.btn_transfer_selected.setEnabled(self._reachable and count > 0)

    # --- Sortieren und Filtern -------------------------------------------------------

    def _header_menu(self, column: int):
        if column == COL_CHECK:
            return
        menu = QMenu(self)
        up = menu.addAction("Aufsteigend sortieren")
        up.triggered.connect(lambda: self._sort(column, Qt.SortOrder.AscendingOrder))
        down = menu.addAction("Absteigend sortieren")
        down.triggered.connect(lambda: self._sort(column, Qt.SortOrder.DescendingOrder))
        values = sorted({self.tree.topLevelItem(r).text(column) for r in range(self.tree.topLevelItemCount())},
                        key=str.casefold)
        if 1 < len(values) <= 30:  # Namen und Nummern sind zu viele - dafuer gibt es das Suchfeld
            menu.addSeparator()
            hidden = self._hidden_values.setdefault(column, set())
            show_all = menu.addAction("Alle anzeigen")
            show_all.triggered.connect(lambda: self.set_value_filter(column, set()))
            for value in values:
                action = menu.addAction(value or "(leer)")
                action.setCheckable(True)
                action.setChecked(value not in hidden)
                action.toggled.connect(lambda on, v=value: self._toggle_value(column, v, on))
        header = self.tree.header()
        menu.exec(header.mapToGlobal(QPoint(header.sectionViewportPosition(column), header.height())))

    def _sort(self, column: int, order):
        self.tree.sortItems(column, order)
        self.tree.header().setSortIndicatorShown(True)
        self.tree.header().setSortIndicator(column, order)

    def _toggle_value(self, column: int, value: str, shown: bool):
        hidden = set(self._hidden_values.get(column, set()))
        (hidden.discard if shown else hidden.add)(value)
        self.set_value_filter(column, hidden)

    def set_value_filter(self, column: int, hidden_values):
        """Werte einer Spalte ausblenden (leere Menge: alle zeigen)"""
        self._hidden_values[column] = set(hidden_values)
        label = CARD_HEADERS[column] + (FILTER_MARK if hidden_values else "")
        self.tree.headerItem().setText(column, label)
        self._apply_filter()

    def _apply_filter(self, *_):
        needle = self.filter_edit.text().strip().casefold()
        for row in range(self.tree.topLevelItemCount()):
            item = self.tree.topLevelItem(row)
            text = (item.text(COL_NAME) + " " + str(item.data(0, FOLDER_ROLE))).casefold()
            hidden = bool(needle) and needle not in text
            for column, values in self._hidden_values.items():
                if item.text(column) in values:
                    hidden = True
            item.setHidden(hidden)


def album_folder_names(paths: List[str], index) -> List[str]:
    """Ordnernamen, unter denen die Alben auf der SD-Karte landen wuerden"""
    names = []
    for path in paths:
        album = index.find(path) if index is not None else None
        if album is not None:
            names.append(safe_name(album.name))
    return names


def in_folder(path: str, folder: str) -> bool:
    """path ist der Ordner selbst oder liegt darin ("/A/B" in "/A")"""
    path, folder = path.strip("/").casefold(), folder.strip("/").casefold()
    return bool(path) and (path == folder or path.startswith(folder + "/"))


class FolderCardsTree(QTreeWidget):
    """Karten, die auf einen Ordner zeigen (Kartenseite und Ordnerseite)"""
    card_selected = pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("trackList")
        self.setRootIsDecorated(False)
        self.setHeaderLabels(["Karte", "Nummer", "Auf diesem ESPuino", "Cover"])
        header = self.header()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        for column in range(1, 4):
            header.setSectionResizeMode(column, QHeaderView.ResizeMode.ResizeToContents)
        self.itemClicked.connect(lambda item, _col: self.card_selected.emit(item.data(0, FOLDER_ROLE)))
        self.itemActivated.connect(lambda item, _col: self.card_selected.emit(item.data(0, FOLDER_ROLE)))

    def set_cards(self, folder: str, cards: List[MediahubCard], has_cover: Dict[str, bool], current: str = ""):
        self.clear()
        for card in cards:
            if card.device_target:  # Zuweisung auf dem verbundenen ESPuino
                if same_folder(card.device_target, folder):
                    here = "zugewiesen"
                elif in_folder(card.device_target, folder):
                    here = "zugewiesen: " + card.device_target
                else:
                    here = f"zeigt auf {card.device_target}"
            elif card.device_status == NOT_ASSIGNED:
                here = "nicht zugewiesen"
            else:
                here = "?"  # nicht verbunden
            item = QTreeWidgetItem([card.label, format_card_id(card.card_id), here,
                                    "✓" if has_cover.get(card.card_id) else "–"])
            item.setData(0, FOLDER_ROLE, card.card_id)
            if not here.startswith("zugewiesen"):
                item.setForeground(2, QBrush(QColor(theme.YELLOW)))
            if card.card_id == current:
                font = item.font(0)
                font.setBold(True)
                for column in range(4):
                    item.setFont(column, font)
            self.addTopLevelItem(item)
            if card.card_id == current:
                self.setCurrentItem(item)
        rows = max(1, len(cards))
        self.setFixedHeight(self.header().sizeHint().height() + rows * max(24, self.sizeHintForRow(0)) + 6)


def add_file_delete_menu(file_list: QListWidget, enabled, signal):
    """Rechtsklick bzw. Entf in einer Dateiliste: "Von der SD-Karte loeschen" -> signal(Zeile)"""
    def request():
        row = file_list.currentRow()
        if enabled() and row >= 0:
            signal.emit(row)

    def menu(pos):
        item = file_list.itemAt(pos)
        if item is None or not enabled():
            return
        file_list.setCurrentItem(item)
        popup = QMenu(file_list)
        popup.addAction("Von der SD-Karte löschen …\tEntf").triggered.connect(request)
        popup.exec(file_list.viewport().mapToGlobal(pos))

    file_list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
    file_list.customContextMenuRequested.connect(menu)
    key = QShortcut(QKeySequence(QKeySequence.StandardKey.Delete), file_list)
    key.setContext(Qt.ShortcutContext.WidgetShortcut)
    key.activated.connect(request)


class FolderPage(QWidget):
    """Ein Ordner auf der SD-Karte des ESPuino: Album und alle Karten, die darauf zeigen"""
    back_requested = pyqtSignal()
    card_selected = pyqtSignal(str)
    assign_requested = pyqtSignal(str)  # "/Ordner"
    cover_requested = pyqtSignal(str)  # "/Ordner": Cover-Manager fuer das Album bzw. die Karte
    delete_requested = pyqtSignal(str)  # "/Ordner" loeschen
    delete_file_requested = pyqtSignal(int)  # Zeile der Dateiliste

    def __init__(self, parent=None):
        super().__init__(parent)
        self.folder = ""
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(12)
        back = QPushButton("← Alle Karten")
        back.setObjectName("ghostButton")
        back.clicked.connect(self.back_requested)
        back_row = QHBoxLayout()
        back_row.addWidget(back)
        back_row.addStretch(1)
        layout.addLayout(back_row)
        head = QFrame()
        head.setObjectName("heroCard")
        head_layout = QHBoxLayout(head)
        head_layout.setContentsMargins(20, 16, 20, 16)
        head_layout.setSpacing(20)
        self.preview = QLabel()
        self.preview.setFixedSize(FOLDER_PREVIEW, FOLDER_PREVIEW)
        self.preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
        head_layout.addWidget(self.preview)
        info = QVBoxLayout()
        info.setSpacing(4)
        self.title_label = QLabel()
        self.title_label.setObjectName("folderTitle")
        self.title_label.setWordWrap(True)
        info.addWidget(self.title_label)
        self.path_label = QLabel()
        self.path_label.setObjectName("folderSubtitle")
        self.path_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        info.addWidget(self.path_label)
        self.meta_label = QLabel()
        self.meta_label.setObjectName("folderMeta")
        self.meta_label.setWordWrap(True)
        info.addWidget(self.meta_label)
        info.addStretch(1)
        actions = QHBoxLayout()
        self.btn_assign = QPushButton("Karte zuweisen …")
        self.btn_assign.setObjectName("primaryButton")
        self.btn_assign.setToolTip("Eine weitere Karte auf den ESPuino legen und diesem Ordner zuweisen")
        self.btn_assign.clicked.connect(lambda: self.assign_requested.emit(self.folder))
        actions.addWidget(self.btn_assign)
        self.btn_cover = QPushButton("Cover-Manager …")
        self.btn_cover.setToolTip("Kartencover für das Album dieses Ordners suchen, per KI erzeugen und gestalten")
        self.btn_cover.clicked.connect(lambda: self.cover_requested.emit(self.folder))
        actions.addWidget(self.btn_cover)
        self.btn_delete = QPushButton("Ordner löschen …")
        self.btn_delete.setObjectName("ghostButton")
        self.btn_delete.setToolTip("Ordner samt Inhalt von der SD-Karte des ESPuino löschen (mit Rückfrage)")
        self.btn_delete.clicked.connect(lambda: self.delete_requested.emit(self.folder))
        actions.addWidget(self.btn_delete)
        actions.addStretch(1)
        info.addLayout(actions)
        head_layout.addLayout(info, 1)
        layout.addWidget(head)
        self.cards_title = QLabel("Karten für diesen Ordner")
        self.cards_title.setObjectName("sectionTitle")
        layout.addWidget(self.cards_title)
        self.tree = FolderCardsTree()
        self.tree.card_selected.connect(self.card_selected)
        layout.addWidget(self.tree)
        self.empty_label = QLabel("Noch keine Karte zeigt auf diesen Ordner.")
        self.empty_label.setStyleSheet(f"color: {theme.OVERLAY2};")
        layout.addWidget(self.empty_label)
        files_title = QLabel("Dateien")
        files_title.setObjectName("sectionTitle")
        layout.addWidget(files_title)
        self.file_list = QListWidget()
        self.file_list.setObjectName("trackList")
        self.can_delete_files = False
        add_file_delete_menu(self.file_list, lambda: self.can_delete_files, self.delete_file_requested)
        layout.addWidget(self.file_list, 1)

    def show_folder(self, folder: str, meta: List[str], preview_path: str, cards: List[MediahubCard],
                    has_cover: Dict[str, bool], can_assign: bool, can_cover: bool = False,
                    files: Optional[List[str]] = None, can_delete: bool = False):
        """cards: bekannte Karten, die auf diesen Ordner zeigen (hier oder laut Verzeichnis)"""
        self.folder = folder
        self.btn_delete.setEnabled(can_delete)
        self.can_delete_files = can_delete and bool(files)
        self.title_label.setText(folder.strip("/").rpartition("/")[2] or folder)
        self.path_label.setText(f"Ordner auf der SD-Karte: {folder}")
        self.meta_label.setText("<br>".join(html.escape(line) for line in meta))
        pixmap = QPixmap(preview_path) if preview_path else QPixmap()
        if pixmap.isNull():
            self.preview.setPixmap(QPixmap())
            self.preview.setText("kein Bild")
            self.preview.setStyleSheet(f"color: {theme.OVERLAY2}; border: 1px dashed {theme.OVERLAY2};"
                                       "border-radius: 8px;")
        else:
            self.preview.setStyleSheet("")
            self.preview.setPixmap(pixmap.scaled(self.preview.size(), Qt.AspectRatioMode.KeepAspectRatio,
                                                 Qt.TransformationMode.SmoothTransformation))
        self.btn_assign.setEnabled(can_assign)
        self.btn_cover.setEnabled(can_cover)
        self.btn_cover.setToolTip("" if can_cover else "Weder ein Album in der Bibliothek noch eine Karte mit "
                                  "Dateien gefunden")
        self.tree.set_cards(folder, cards, has_cover)
        self.tree.setVisible(bool(cards))
        self.file_list.clear()
        for name in files or []:
            self.file_list.addItem(name)
        if files is not None and not files:
            self.file_list.addItem("Keine Audiodateien gefunden")
        if files is None:
            self.file_list.addItem("Dateien unbekannt – kein Album in der Bibliothek, "
                                   "und die SD-Karte steckt nicht im PC")
        count = len(cards)
        self.cards_title.setText(f"Karten für diesen Ordner ({count})")
        self.empty_label.setVisible(count == 0)
