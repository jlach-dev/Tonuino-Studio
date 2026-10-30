"""
Einstellungen der Bibliothek: Quellordner (PC, NAS) hinzufuegen und entfernen
"""

import os
from typing import List, Optional

from PyQt6.QtCore import QSettings, Qt
from PyQt6.QtWidgets import (
    QDialog, QDialogButtonBox, QHBoxLayout, QLabel, QListWidget, QPushButton, QVBoxLayout,
)

from core.library import SETTINGS_SOURCES, SETTINGS_WRITE_ID_TAGS, is_offline
from gui.common import pick_directories


def source_conflict(path: str, existing: List[str]) -> Optional[str]:
    """Warum ein Ordner nicht als weitere Quelle taugt (None = passt)"""
    norm = os.path.normcase(os.path.normpath(path))
    for other in existing:
        other_norm = os.path.normcase(os.path.normpath(other))
        if norm == other_norm:
            return "ist schon eingetragen"
        if norm.startswith(other_norm.rstrip("\\/") + os.sep):
            return f"liegt schon innerhalb von „{other}“"
        if other_norm.startswith(norm.rstrip("\\/") + os.sep):
            return f"enthält die vorhandene Quelle „{other}“ – bitte diese entfernen"
    return None


class SourcesList(QListWidget):
    """Quellenliste, die Ordner aus dem Explorer annimmt"""

    def __init__(self, on_drop, parent=None):
        super().__init__(parent)
        self._on_drop = on_drop
        self.setAcceptDrops(True)

    def _folders(self, event) -> List[str]:
        mime = event.mimeData()
        if not mime.hasUrls():
            return []
        return [os.path.normpath(u.toLocalFile()) for u in mime.urls()
                if u.isLocalFile() and os.path.isdir(u.toLocalFile())]

    def dragEnterEvent(self, event):
        if self._folders(event):
            event.setDropAction(Qt.DropAction.CopyAction)
            event.accept()
        else:
            event.ignore()

    def dragMoveEvent(self, event):
        self.dragEnterEvent(event)

    def dropEvent(self, event):
        folders = self._folders(event)
        if not folders:
            event.ignore()
            return
        event.accept()
        self._on_drop(folders)


def library_sources() -> List[str]:
    value = QSettings().value(SETTINGS_SOURCES, [], type=list)
    return [str(v) for v in value if str(v).strip()]


def save_library_sources(sources: List[str]):
    QSettings().setValue(SETTINGS_SOURCES, list(sources))


class LibrarySourcesDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Bibliothek-Ordner")
        self.setMinimumSize(560, 360)
        layout = QVBoxLayout(self)
        layout.setSpacing(12)

        intro = QLabel("Ordner, in denen deine Hörspiele und deine Musik liegen (PC, NAS). Jeder Unterordner mit "
                       "Audiodateien gilt als Album; CD1/CD2-Unterordner werden zusammengefasst. Die Dateien "
                       "bleiben, wo sie sind. Mehrere Ordner auf einmal: aus dem Explorer in die Liste ziehen.")
        intro.setWordWrap(True)
        layout.addWidget(intro)

        self.list = SourcesList(self.add_sources)
        for source in library_sources():
            self._add_item(source)
        layout.addWidget(self.list, 1)

        buttons = QHBoxLayout()
        btn_add = QPushButton("Ordner hinzufügen …")
        btn_add.setObjectName("primaryButton")
        btn_add.clicked.connect(self._add)
        buttons.addWidget(btn_add)
        self.btn_remove = QPushButton("Entfernen")
        self.btn_remove.setObjectName("ghostButton")
        self.btn_remove.clicked.connect(self._remove)
        buttons.addWidget(self.btn_remove)
        buttons.addStretch(1)
        layout.addLayout(buttons)

        self.note_label = QLabel()
        self.note_label.setObjectName("folderMeta")
        self.note_label.setWordWrap(True)
        layout.addWidget(self.note_label)

        self.tag_label = QLabel()
        self.tag_label.setObjectName("folderMeta")
        self.tag_label.setWordWrap(True)
        layout.addWidget(self.tag_label)
        self.btn_reset_tags = QPushButton("Entscheidung zurücksetzen")
        self.btn_reset_tags.setObjectName("ghostButton")
        self.btn_reset_tags.clicked.connect(self._reset_tag_decision)
        layout.addWidget(self.btn_reset_tags, 0)
        self._update_tag_label()

        box = QDialogButtonBox()
        box.addButton("Übernehmen", QDialogButtonBox.ButtonRole.AcceptRole)
        box.addButton("Abbrechen", QDialogButtonBox.ButtonRole.RejectRole)
        box.accepted.connect(self.accept)
        box.rejected.connect(self.reject)
        layout.addWidget(box)

    def _add_item(self, source: str):
        text = source + ("   (nicht erreichbar)" if is_offline(source) else "")
        self.list.addItem(text)
        self.list.item(self.list.count() - 1).setData(0x0100, source)

    def _add(self):
        self.add_sources(pick_directories(self, "Bibliothek-Ordner wählen"))

    def add_sources(self, paths: List[str]) -> List[str]:
        """Fuegt Ordner hinzu; Duplikate und verschachtelte Ordner werden gemeldet. Gibt die neuen zurueck."""
        added, skipped = [], []
        for path in paths:
            reason = source_conflict(path, self.sources + added)
            if reason:
                skipped.append(f"{path}: {reason}")
                continue
            self._add_item(path)
            added.append(path)
        if skipped:
            self.note_label.setText("Übersprungen: " + "; ".join(skipped))
        elif added:
            self.note_label.setText(f"{len(added)} Ordner hinzugefügt.")
        return added

    def _remove(self):
        for item in self.list.selectedItems():
            self.list.takeItem(self.list.row(item))

    def _update_tag_label(self):
        decision = QSettings().value(SETTINGS_WRITE_ID_TAGS, "", type=str)
        text = {"yes": "Album-IDs werden in die Bibliotheksdateien geschrieben (einmalig erlaubt).",
                "no": "Album-IDs werden nicht in die Bibliotheksdateien geschrieben (abgelehnt)."}.get(
            decision, "Ob die Album-ID auch in die Bibliotheksdateien geschrieben werden darf, wird beim ersten "
                      "Import gefragt.")
        self.tag_label.setText(text)
        self.btn_reset_tags.setVisible(bool(decision))

    def _reset_tag_decision(self):
        QSettings().remove(SETTINGS_WRITE_ID_TAGS)
        self._update_tag_label()

    @property
    def sources(self) -> List[str]:
        return [self.list.item(i).data(0x0100) for i in range(self.list.count())]

    def accept(self):
        save_library_sources(self.sources)
        super().accept()


def library_id_tags_allowed() -> bool:
    """Darf die Album-ID in die Originaldateien der Bibliothek geschrieben werden?
    Nur nach ausdruecklicher Zustimmung (gefragt beim ersten Import)."""
    return QSettings().value(SETTINGS_WRITE_ID_TAGS, "", type=str) == "yes"
