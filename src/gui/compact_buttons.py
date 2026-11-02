"""
Knopfleiste, die bei wenig Platz auf Symbole schrumpft

Im Ordnerkopf stehen vier Knoepfe nebeneinander (Original: Tracks hinzufuegen,
Lautstaerke angleichen, Ordner loeschen; Fork: Cover-Manager). Bei schmalen
Fenstern sind sie zusammen breiter als der Platz neben dem Cover und
ueberlappen sich. Dann zeigen die ausgewaehlten Knoepfe nur ihr Symbol, der
Text steht im Tooltip. Reicht das nicht, schrumpfen auch die `later` genannten.
"""

from typing import List, Optional

from PyQt6.QtCore import QEvent, QObject, QTimer
from PyQt6.QtWidgets import QBoxLayout, QPushButton, QWidget


class CompactButtons(QObject):
    """Beobachtet die Breite von `container` (dem Widget, das die Knopfleiste
    `layout` enthaelt) und schaltet `compactable` bei Platzmangel auf Symbole,
    bei noch weniger Platz auch `later`"""

    def __init__(self, container: QWidget, layout: QBoxLayout, compactable: List[QPushButton],
                 later: Optional[List[QPushButton]] = None):
        super().__init__(container)
        self.container = container
        self.layout = layout
        self.stages = [list(compactable), list(compactable) + list(later or [])]
        compactable = self.stages[1]
        self.labels = {button: button.text() for button in compactable}
        self.tooltips = {button: button.toolTip() for button in compactable}
        self.level = 0  # 0: alle Texte, 1: compactable als Symbol, 2: auch later
        # Mindestbreite = nur Symbol: sonst rechnet Qt fuer die Mindestbreite des
        # Fensters weiter mit den Texten, und Fenster bzw. Trenner lassen sich gar
        # nicht erst so schmal ziehen, dass der Kompaktmodus greift
        for button in compactable:
            button.setText("")
            icon_only = button.sizeHint().width()
            button.setText(self.labels[button])
            button.setMinimumWidth(icon_only)
        self.icon_only_width = {button: button.minimumWidth() for button in compactable}
        container.installEventFilter(self)

    @property
    def compact(self) -> bool:
        return self.level > 0

    def full_width(self, as_icons=()) -> int:
        """Breite der Knopfleiste mit allen Texten, as_icons nur als Symbol"""
        widths = []
        for i in range(self.layout.count()):
            widget = self.layout.itemAt(i).widget()
            if widget is None or not widget.isVisibleTo(self.container):
                continue
            if widget in as_icons:
                widths.append(self.icon_only_width[widget])
            elif widget in self.labels:
                widths.append(max(self.icon_only_width[widget],
                                  widget.fontMetrics().horizontalAdvance(self.labels[widget])
                                  + widget.iconSize().width() + 40))
            else:
                widths.append(max(widget.minimumWidth(), widget.sizeHint().width()))
        return sum(widths) + self.layout.spacing() * max(len(widths) - 1, 0)

    def update_mode(self):
        available = self.layout.geometry().width() or self.container.width()
        if self.full_width() <= available:
            self.set_level(0)
        else:
            self.set_level(1 if self.full_width(self.stages[0]) <= available else 2)

    def set_level(self, level: int):
        if level == self.level:
            return
        self.level = level
        icons = self.stages[level - 1] if level else []
        for button in self.stages[1]:
            compact = button in icons
            label = self.labels[button].strip()
            button.setText("" if compact else self.labels[button])
            tip = self.tooltips[button]
            button.setToolTip(f"{label} – {tip}" if compact and tip else (label if compact else tip))

    def eventFilter(self, obj, event):
        if obj is self.container and event.type() in (QEvent.Type.Resize, QEvent.Type.Show,
                                                      QEvent.Type.LayoutRequest):
            # Erst nach dem Neu-Layouten messen, sonst stimmt die Breite der Leiste noch nicht
            QTimer.singleShot(0, self.update_mode)
        return False
