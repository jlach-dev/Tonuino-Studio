"""
Ansicht "ESPuino-Karten" im ESPuino-Modus (siehe core/app_mode.py): Seitenleiste
mit Kartenliste und Kartenseite. Die Karten kommen direkt vom ESPuino
(core/espuino_device.py) oder aus dem Mediahub (core/mediahub_cards.py); die
Dialoge stehen in gui/espuino_dialogs.py.

Eigene Datei des Forks (siehe core/branding.py).
"""

from typing import Dict, List, Optional

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtGui import QBrush, QColor, QPixmap
from PyQt6.QtWidgets import (
    QFrame, QHBoxLayout, QLabel, QLineEdit, QListWidget, QListWidgetItem, QPushButton, QStackedWidget, QVBoxLayout,
    QWidget,
)

from core.mediahub_cards import MediahubCard, MediahubFolder, format_card_id, play_mode_label
from gui import theme
from gui.common import heading_font

PREVIEW_HEIGHT = 240
CARD_ROLE = Qt.ItemDataRole.UserRole


class MediahubSidebar(QWidget):
    """Seitenleiste im ESPuino-Modus: Einrichten, Neu einlesen, Kartenliste"""
    setup_requested = pyqtSignal()
    assign_requested = pyqtSignal()
    reload_requested = pyqtSignal()
    card_selected = pyqtSignal(str)  # card_id

    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(12)
        self.btn_setup = QPushButton("ESPuino einrichten …")
        self.btn_setup.setToolTip("Karten direkt vom ESPuino im WLAN oder aus dem ESPuino-Mediahub lesen")
        self.btn_setup.clicked.connect(self.setup_requested)
        layout.addWidget(self.btn_setup)
        self.btn_assign = QPushButton("Karte zuweisen …")
        self.btn_assign.setObjectName("primaryButton")
        self.btn_assign.setToolTip("Karte auf den ESPuino legen und einen Ordner der SD-Karte zuweisen")
        self.btn_assign.clicked.connect(self.assign_requested)
        self.btn_assign.hide()
        layout.addWidget(self.btn_assign)
        self.btn_reload = QPushButton("Karten neu einlesen")
        self.btn_reload.setToolTip("Karten erneut lesen, z. B. nachdem anderswo Karten zugewiesen wurden")
        self.btn_reload.clicked.connect(self.reload_requested)
        layout.addWidget(self.btn_reload)
        self.filter_edit = QLineEdit()
        self.filter_edit.setPlaceholderText("Karten filtern …")
        self.filter_edit.setClearButtonEnabled(True)
        self.filter_edit.textChanged.connect(self._apply_filter)
        layout.addWidget(self.filter_edit)
        self.count_label = QLabel("Karten:")
        layout.addWidget(self.count_label)
        self.card_list = QListWidget()
        self.card_list.setObjectName("folderList")
        self.card_list.currentItemChanged.connect(self._on_current_changed)
        layout.addWidget(self.card_list, 1)

    def set_cards(self, cards: List[MediahubCard], has_cover: Dict[str, bool], select: str = ""):
        self.card_list.blockSignals(True)
        self.card_list.clear()
        for card in cards:
            item = QListWidgetItem()
            item.setData(CARD_ROLE, card.card_id)
            self.card_list.addItem(item)
            self._update_item(item, card, has_cover.get(card.card_id, False))
        self.card_list.blockSignals(False)
        missing = sum(1 for c in cards if not has_cover.get(c.card_id))
        self.count_label.setText(f"Karten: {len(cards)}" + (f" ({missing} ohne Cover, grau)" if missing else ""))
        self._apply_filter()
        if select:
            self.select(select)

    def _update_item(self, item: QListWidgetItem, card: MediahubCard, has_cover: bool):
        # Karten ohne Cover grau und kursiv (wie in der Zeile "Karten: … ohne Cover" erklaert)
        item.setText(card.label)
        font = item.font()
        font.setItalic(not has_cover)
        item.setFont(font)
        item.setForeground(QBrush(QColor(theme.TEXT if has_cover else theme.OVERLAY2)))
        item.setToolTip(f"Karte {format_card_id(card.card_id)}"
                        + (" – Kartencover vorhanden" if has_cover else " – noch kein Kartencover"))
        item.setData(Qt.ItemDataRole.UserRole + 1, card.label.casefold())

    def set_has_cover(self, card: MediahubCard, has_cover: bool):
        item = self._item(card.card_id)
        if item is not None:
            self._update_item(item, card, has_cover)

    def _item(self, card_id: str) -> Optional[QListWidgetItem]:
        for row in range(self.card_list.count()):
            item = self.card_list.item(row)
            if item.data(CARD_ROLE) == card_id:
                return item
        return None

    def select(self, card_id: str):
        item = self._item(card_id)
        if item is not None:
            self.card_list.setCurrentItem(item)

    def current_card_id(self) -> str:
        item = self.card_list.currentItem()
        return item.data(CARD_ROLE) if item is not None else ""

    def _apply_filter(self, *_):
        needle = self.filter_edit.text().strip().casefold()
        for row in range(self.card_list.count()):
            item = self.card_list.item(row)
            item.setHidden(bool(needle) and needle not in (item.data(Qt.ItemDataRole.UserRole + 1) or "")
                           and needle not in (item.data(CARD_ROLE) or ""))

    def _on_current_changed(self, current, _previous):
        if current is not None:
            self.card_selected.emit(current.data(CARD_ROLE))


class MediahubPage(QWidget):
    """Hauptbereich: Uebersicht bzw. die gewaehlte Karte"""
    setup_requested = pyqtSignal()
    cover_requested = pyqtSignal(str)  # card_id
    batch_requested = pyqtSignal()
    print_requested = pyqtSignal()
    transfer_requested = pyqtSignal(str)  # card_id: Album auf diesen ESPuino bringen und zuweisen
    back_requested = pyqtSignal()  # von der Karte zurueck zur Kartentabelle (ESPuino direkt)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.card: Optional[MediahubCard] = None
        self.pages = QStackedWidget()
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(self.pages)
        self.pages.addWidget(self._build_overview())
        self.pages.addWidget(self._build_card())
        self.pages.addWidget(self._build_device_overview())
        from gui.espuino_device_view import FolderPage
        self.folder_page = FolderPage()  # Ordner auf der SD-Karte des ESPuino (Index 3)
        self.folder_page.back_requested.connect(self.back_requested)
        self.pages.addWidget(self.folder_page)
        self.device_mode = False

    def _build_overview(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.overview_title = QLabel("ESPuino-Karten")
        self.overview_title.setFont(heading_font(18))
        self.overview_title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self.overview_title)
        self.overview_text = QLabel()
        self.overview_text.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.overview_text.setWordWrap(True)
        layout.addWidget(self.overview_text)
        row = QHBoxLayout()
        row.addStretch(1)
        self.btn_overview_setup = QPushButton("ESPuino einrichten …")
        self.btn_overview_setup.setObjectName("primaryButton")
        self.btn_overview_setup.clicked.connect(self.setup_requested)
        row.addWidget(self.btn_overview_setup)
        self.btn_overview_batch = QPushButton("Kartencover für alle Karten ohne Cover …")
        self.btn_overview_batch.clicked.connect(self.batch_requested)
        row.addWidget(self.btn_overview_batch)
        self.btn_overview_print = QPushButton("Druckbogen …")
        self.btn_overview_print.clicked.connect(self.print_requested)
        row.addWidget(self.btn_overview_print)
        row.addStretch(1)
        layout.addLayout(row)
        return page

    def _build_device_overview(self) -> QWidget:
        from gui.espuino_device_view import KnownCardsTable
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)
        title = QLabel("ESPuino-Karten")
        title.setFont(heading_font(18))
        layout.addWidget(title)
        self.device_text = QLabel()
        self.device_text.setWordWrap(True)
        layout.addWidget(self.device_text)
        self.cards_table = KnownCardsTable()
        layout.addWidget(self.cards_table, 1)
        row = QHBoxLayout()
        self.btn_device_batch = QPushButton("Kartencover für alle Karten ohne Cover …")
        self.btn_device_batch.clicked.connect(self.batch_requested)
        row.addWidget(self.btn_device_batch)
        self.btn_device_print = QPushButton("Druckbogen …")
        self.btn_device_print.clicked.connect(self.print_requested)
        row.addWidget(self.btn_device_print)
        row.addStretch(1)
        layout.addLayout(row)
        return page

    def show_device_overview(self, text: str, can_batch: bool, can_print: bool):
        self.card = None
        self.device_text.setText(text)
        self.btn_device_batch.setEnabled(can_batch)
        self.btn_device_print.setEnabled(can_print)
        self.pages.setCurrentIndex(2)

    def _build_card(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(12)
        self.btn_back = QPushButton("← Alle Karten")
        self.btn_back.setObjectName("ghostButton")
        self.btn_back.clicked.connect(self.back_requested)
        self.btn_back.hide()
        back_row = QHBoxLayout()
        back_row.addWidget(self.btn_back)
        back_row.addStretch(1)
        layout.addLayout(back_row)
        head = QFrame()
        head.setObjectName("heroCard")
        head_layout = QHBoxLayout(head)
        head_layout.setContentsMargins(20, 16, 20, 16)
        head_layout.setSpacing(20)
        self.preview = QLabel()
        self.preview.setFixedSize(PREVIEW_HEIGHT, PREVIEW_HEIGHT)
        self.preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.preview.setWordWrap(True)
        head_layout.addWidget(self.preview)
        info = QVBoxLayout()
        info.setSpacing(4)
        self.title_label = QLabel()
        self.title_label.setObjectName("folderTitle")
        self.title_label.setWordWrap(True)
        info.addWidget(self.title_label)
        self.id_label = QLabel()
        self.id_label.setObjectName("folderSubtitle")
        self.id_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        info.addWidget(self.id_label)
        self.meta_label = QLabel()
        self.meta_label.setObjectName("folderMeta")
        self.meta_label.setWordWrap(True)
        info.addWidget(self.meta_label)
        self.warning_label = QLabel()
        self.warning_label.setWordWrap(True)
        self.warning_label.setStyleSheet(f"color: {theme.YELLOW};")
        info.addWidget(self.warning_label)
        self.status_label = QLabel()
        self.status_label.setWordWrap(True)
        self.status_label.hide()
        info.addWidget(self.status_label)
        info.addStretch(1)
        actions = QHBoxLayout()
        self.btn_cover = QPushButton("Cover-Manager …")
        self.btn_cover.setObjectName("primaryButton")
        self.btn_cover.setToolTip("Kartencover für diese Karte suchen, per KI erzeugen und gestalten")
        self.btn_cover.clicked.connect(lambda: self.card and self.cover_requested.emit(self.card.card_id))
        actions.addWidget(self.btn_cover)
        self.btn_transfer = QPushButton("Auf diesen ESPuino übertragen")
        self.btn_transfer.clicked.connect(lambda: self.card and self.transfer_requested.emit(self.card.card_id))
        self.btn_transfer.hide()
        actions.addWidget(self.btn_transfer)
        actions.addStretch(1)
        info.addLayout(actions)
        head_layout.addLayout(info, 1)
        layout.addWidget(head)

        files_title = QLabel("Dateien")
        files_title.setObjectName("sectionTitle")
        layout.addWidget(files_title)
        self.file_list = QListWidget()
        self.file_list.setObjectName("trackList")
        layout.addWidget(self.file_list, 1)
        return page

    def show_folder(self, *args, **kwargs):
        self.card = None
        self.folder_page.show_folder(*args, **kwargs)
        self.pages.setCurrentWidget(self.folder_page)

    def show_overview(self, text: str, configured: bool, can_batch: bool = False, can_print: bool = False):
        self.card = None
        self.overview_text.setText(text)
        self.btn_overview_setup.setVisible(not configured)
        self.btn_overview_batch.setVisible(configured)
        self.btn_overview_batch.setEnabled(can_batch)
        self.btn_overview_print.setVisible(configured)
        self.btn_overview_print.setEnabled(can_print)
        self.pages.setCurrentIndex(0)

    def show_card(self, card: MediahubCard, folder: MediahubFolder, place: str = "in der Mediathek"):
        self.card = card
        self.title_label.setText(card.label)
        self.id_label.setText(f"Karte {format_card_id(card.card_id)}")
        devices = ", ".join(d.label for d in card.devices) or "–"
        count = len(card.files)
        files = f"{count} Datei" + ("" if count == 1 else "en")
        if folder.missing:
            files += f", davon {folder.missing} nicht {place} gefunden"
        meta = [f"ESPuino: {devices}", files]
        if card.sd_path:
            meta.insert(1, f"SD-Karte: {card.sd_path}")
        if card.play_mode:
            meta.append(f"Abspielmodus: {play_mode_label(card.play_mode)}")
        self.meta_label.setText(" · ".join(meta))
        warnings = []
        if card.conflict:
            warnings.append("Die Karte ist auf mehreren ESPuinos unterschiedlich belegt – gezeigt ist die "
                            "neueste Zuweisung.")
        if not folder.tracks:
            warnings.append(f"Keine Datei dieser Karte {place} gefunden – ist der Ordner unter "
                            "„ESPuino einrichten“ richtig?")
        self.warning_label.setText("\n".join(warnings))
        self.warning_label.setVisible(bool(warnings))
        self.btn_cover.setEnabled(bool(folder.tracks))
        self.set_preview(folder.card_path)
        self.btn_back.setVisible(self.device_mode)
        self.status_label.setVisible(self.device_mode)
        self.btn_transfer.setVisible(self.device_mode)
        if self.device_mode:
            self._show_device_status(card)

        self.file_list.clear()
        missing = set(folder.missing_files)
        for rel in card.files:
            item = QListWidgetItem(rel)
            if rel in missing:
                item.setText(f"{rel}   (fehlt)")
                item.setForeground(QBrush(QColor(theme.OVERLAY2)))
            self.file_list.addItem(item)
        self.pages.setCurrentIndex(1)

    def _show_device_status(self, card: MediahubCard):
        from core.espuino_cards import ASSIGNED, ASSIGNED_OTHER, NOT_ASSIGNED
        from gui.espuino_device_view import can_transfer, needs_transfer
        parts = {ASSIGNED: "Auf diesem ESPuino zugewiesen",
                 NOT_ASSIGNED: "Auf diesem ESPuino nicht zugewiesen",
                 ASSIGNED_OTHER: f"Auf diesem ESPuino anders belegt: {card.device_target}",
                 "": "ESPuino nicht erreichbar"}
        text = [parts.get(card.device_status, "")]
        text.append({True: "Ordner auf der SD-Karte vorhanden", False: "Ordner fehlt auf der SD-Karte",
                     None: "Ordner unbekannt"}[card.folder_present])
        text.append("Album in der Bibliothek: " + (card.library_path or "nicht gefunden"))
        warn = needs_transfer(card) or card.folder_present is False
        self.status_label.setText(" · ".join(text[:2]) + "\n" + text[2])
        self.status_label.setStyleSheet(f"color: {theme.YELLOW};" if warn else "")
        self.btn_transfer.setEnabled(can_transfer(card) and card.device_status != "")
        if not card.library_path:
            self.btn_transfer.setToolTip("Das Hörspiel ist nicht in der Bibliothek – zuerst dort einlesen")
        elif not can_transfer(card):
            self.btn_transfer.setToolTip("Karten mit einer einzelnen Datei lassen sich hier nicht übertragen")
        else:
            self.btn_transfer.setToolTip(f"Album nach {card.sd_path} übertragen (fehlende Dateien) "
                                         "und die Karte auf diesem ESPuino zuweisen")

    def set_preview(self, card_path: str):
        pixmap = QPixmap(card_path) if card_path else QPixmap()
        if pixmap.isNull():
            self.preview.setPixmap(QPixmap())
            self.preview.setText("Noch kein\nKartencover")
            self.preview.setStyleSheet(f"color: {theme.OVERLAY2}; border: 1px dashed {theme.OVERLAY2};"
                                       "border-radius: 8px;")
            return
        self.preview.setStyleSheet("")
        ratio = self.devicePixelRatioF()
        scaled = pixmap.scaled(round(PREVIEW_HEIGHT * ratio), round(PREVIEW_HEIGHT * ratio),
                               Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)
        scaled.setDevicePixelRatio(ratio)
        self.preview.setPixmap(scaled)
