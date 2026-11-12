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
from PyQt6.QtGui import QBrush, QColor
from PyQt6.QtWidgets import (
    QAbstractItemView, QHeaderView, QLabel, QLineEdit, QListWidget, QListWidgetItem, QMenu, QPushButton,
    QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget,
)

from core.espuino_device import NOT_CONNECTED
from core.espuino_cards import ASSIGNED, ASSIGNED_OTHER, NOT_ASSIGNED
from core.espuino_transfer import safe_name
from core.mediahub_cards import MediahubCard, format_card_id
from gui import theme
from gui.library_dnd import DropHighlight, drop_payload_from_mime

FOLDER_ROLE = Qt.ItemDataRole.UserRole


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
    read_requested = pyqtSignal()
    reload_requested = pyqtSignal()
    albums_dropped = pyqtSignal(list)
    assign_folder_requested = pyqtSignal(str)  # "/Ordner"

    def __init__(self, parent=None):
        super().__init__(parent)
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
        self.btn_assign = QPushButton("Karte zuweisen …")
        self.btn_assign.setObjectName("primaryButton")
        self.btn_assign.setToolTip("Karte auf den ESPuino legen und einen Ordner der SD-Karte zuweisen")
        self.btn_assign.clicked.connect(self.assign_requested)
        layout.addWidget(self.btn_assign)
        self.btn_read = QPushButton("Karte auslesen …")
        self.btn_read.setToolTip("Karten an den ESPuino halten und sehen, welche es ist – mit Kartencover. "
                                 "Praktisch beim Bekleben")
        self.btn_read.clicked.connect(self.read_requested)
        layout.addWidget(self.btn_read)
        self.btn_reload = QPushButton("Neu einlesen")
        self.btn_reload.setToolTip("Zuweisungen und Ordner erneut vom ESPuino lesen")
        self.btn_reload.clicked.connect(self.reload_requested)
        layout.addWidget(self.btn_reload)
        self.folders_label = QLabel("Auf dem ESPuino:")
        layout.addWidget(self.folders_label)
        self.folder_list = DeviceFolderList()
        self.folder_list.setObjectName("folderList")
        self.folder_list.albums_dropped.connect(self.albums_dropped)
        self.folder_list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.folder_list.customContextMenuRequested.connect(self._folder_menu)
        layout.addWidget(self.folder_list, 1)
        self.drop_label = QLabel("Alben aus der Bibliothek hierher ziehen.")
        self.drop_label.setWordWrap(True)
        self.drop_label.setStyleSheet(f"color: {theme.OVERLAY2};")
        layout.addWidget(self.drop_label)

    def set_state(self, host: str, state, folder_cards: Dict[str, List[str]], name: str = ""):
        """state: core.espuino_device.DeviceState oder None (nicht eingerichtet).
        folder_cards: "/Ordner" (klein) -> Kartennamen, die darauf zeigen.
        name: Name des Geraets in der Liste der ESPuinos"""
        configured = bool(host)
        connected = state is not None and state.error != NOT_CONNECTED
        self.btn_assign.setEnabled(configured and state is not None and state.reachable)
        self.btn_read.setEnabled(self.btn_assign.isEnabled())
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
        self.device_label.setText("<br>".join(lines))
        can_drop = state.reachable or state.folders_from_pc
        self.folder_list.drop_enabled = can_drop
        self.folder_list.drop_hint_blocked = "ESPuino nicht erreichbar" if connected else "Erst verbinden"
        for folder in state.folders or []:
            cards = folder_cards.get(folder.strip("/").casefold(), [])
            item = QListWidgetItem(folder.strip("/"))
            item.setData(FOLDER_ROLE, folder)
            if cards:
                item.setToolTip("Karte: " + ", ".join(cards))
            else:
                item.setForeground(QBrush(QColor(theme.OVERLAY2)))
                item.setToolTip("Keine Karte zeigt auf diesen Ordner")
            self.folder_list.addItem(item)
        if state.folders is None:
            self.folders_label.setText("Auf dem ESPuino: (unbekannt)")
        else:
            self.folders_label.setText(f"Auf dem ESPuino: {len(state.folders)} Ordner")

    def _folder_menu(self, pos: QPoint):
        item = self.folder_list.itemAt(pos)
        if item is None or not self.btn_assign.isEnabled():
            return
        menu = QMenu(self)
        action = menu.addAction("Karte zuweisen …")
        action.triggered.connect(lambda: self.assign_folder_requested.emit(item.data(FOLDER_ROLE)))
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


def needs_transfer(card: MediahubCard) -> bool:
    """Auf dem Geraet unvollstaendig: Ordner fehlt oder Karte nicht (so) zugewiesen"""
    return can_transfer(card) and (card.folder_present is False
                                   or card.device_status in (NOT_ASSIGNED, ASSIGNED_OTHER))


class KnownCardsTable(QWidget):
    """Bekannte Karten mit Status auf dem aktuellen ESPuino"""
    card_selected = pyqtSignal(str)
    transfer_missing_requested = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
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
        self.tree.setHeaderLabels(["Karte", "Nummer", "Zugewiesen", "Ordner", "Cover"])
        header = self.tree.header()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        for column in range(1, 5):
            header.setSectionResizeMode(column, QHeaderView.ResizeMode.ResizeToContents)
        self.tree.itemActivated.connect(lambda item, _col: self.card_selected.emit(item.data(0, FOLDER_ROLE)))
        self.tree.itemClicked.connect(lambda item, _col: self.card_selected.emit(item.data(0, FOLDER_ROLE)))
        layout.addWidget(self.tree, 1)
        self.legend = QLabel("Zugewiesen: ✓ auf diesem ESPuino · – nicht zugewiesen · anders: dort anders belegt. "
                             "Ordner: ✓ auf der SD-Karte · fehlt.")
        self.legend.setWordWrap(True)
        self.legend.setStyleSheet(f"color: {theme.OVERLAY2};")
        layout.addWidget(self.legend)
        self.btn_transfer_missing = QPushButton("Fehlende auf den ESPuino übertragen")
        self.btn_transfer_missing.setObjectName("primaryButton")
        self.btn_transfer_missing.setToolTip("Für jede Karte, deren Ordner fehlt oder die auf diesem ESPuino nicht "
                                             "zugewiesen ist: Album aus der Bibliothek übertragen und zuweisen")
        self.btn_transfer_missing.clicked.connect(self.transfer_missing_requested)
        layout.addWidget(self.btn_transfer_missing)

    def set_cards(self, cards: List[MediahubCard], has_cover: Dict[str, bool], reachable: bool):
        self.tree.clear()
        for card in cards:
            folder = {True: "✓", False: "fehlt", None: "?"}[card.folder_present]
            status = STATUS_TEXT.get(card.device_status, "?")
            item = QTreeWidgetItem([card.label, format_card_id(card.card_id), status, folder,
                                    "✓" if has_cover.get(card.card_id) else "–"])
            item.setData(0, FOLDER_ROLE, card.card_id)
            tips = [f"Ordner: {card.sd_path or '–'}"]
            if card.device_status == ASSIGNED_OTHER:
                tips.append(f"Auf diesem ESPuino zeigt die Karte auf {card.device_target}")
            tips.append("Album in der Bibliothek: " + (card.library_path or "nicht gefunden"))
            item.setToolTip(0, "\n".join(tips))
            warn = card.folder_present is False or card.device_status in (NOT_ASSIGNED, ASSIGNED_OTHER)
            if warn:
                for column in (2, 3):
                    item.setForeground(column, QBrush(QColor(theme.YELLOW)))
            if not has_cover.get(card.card_id):
                font = item.font(0)
                font.setItalic(True)
                item.setFont(0, font)
            self.tree.addTopLevelItem(item)
        missing = sum(1 for c in cards if needs_transfer(c))
        self.btn_transfer_missing.setText(f"Fehlende auf den ESPuino übertragen ({missing})")
        self.btn_transfer_missing.setEnabled(reachable and missing > 0)
        self._apply_filter()

    def _apply_filter(self, *_):
        needle = self.filter_edit.text().strip().casefold()
        for row in range(self.tree.topLevelItemCount()):
            item = self.tree.topLevelItem(row)
            text = (item.text(0) + " " + str(item.data(0, FOLDER_ROLE))).casefold()
            item.setHidden(bool(needle) and needle not in text)


def album_folder_names(paths: List[str], index) -> List[str]:
    """Ordnernamen, unter denen die Alben auf der SD-Karte landen wuerden"""
    names = []
    for path in paths:
        album = index.find(path) if index is not None else None
        if album is not None:
            names.append(safe_name(album.name))
    return names
