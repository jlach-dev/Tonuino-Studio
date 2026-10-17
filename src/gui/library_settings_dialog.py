"""
Einstellungen der Bibliothek: Quellordner (PC, NAS) hinzufuegen und entfernen
"""

from typing import List

from PyQt6.QtCore import QSettings
from PyQt6.QtWidgets import (
    QDialog, QDialogButtonBox, QFileDialog, QHBoxLayout, QLabel, QListWidget, QPushButton, QVBoxLayout,
)

from core.library import SETTINGS_SOURCES, SETTINGS_WRITE_ID_TAGS, is_offline


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
                       "bleiben, wo sie sind.")
        intro.setWordWrap(True)
        layout.addWidget(intro)

        self.list = QListWidget()
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
        path = QFileDialog.getExistingDirectory(self, "Bibliothek-Ordner wählen", "",
                                                QFileDialog.Option.ShowDirsOnly)
        if path and path not in self.sources:
            self._add_item(path)

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
