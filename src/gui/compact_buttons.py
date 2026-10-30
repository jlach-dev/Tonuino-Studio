"""
Knopfleiste, die bei wenig Platz auf Symbole schrumpft

Im Ordnerkopf stehen vier Knoepfe nebeneinander (Original: Tracks hinzufuegen,
Lautstaerke angleichen, Ordner loeschen; Fork: Cover-Manager). Bei schmalen
Fenstern sind sie zusammen breiter als der Platz neben dem Cover und
ueberlappen sich. Dann zeigen die ausgewaehlten Knoepfe nur ihr Symbol, der
Text steht im Tooltip.
"""

from typing import List

from PyQt6.QtCore import QEvent, QObject, QTimer
from PyQt6.QtWidgets import QBoxLayout, QPushButton, QWidget


class CompactButtons(QObject):
    """Beobachtet die Breite von `container` (dem Widget, das die Knopfleiste
    `layout` enthaelt) und schaltet `compactable` bei Platzmangel auf Symbole"""

    def __init__(self, container: QWidget, layout: QBoxLayout, compactable: List[QPushButton]):
        super().__init__(container)
        self.container = container
        self.layout = layout
        self.buttons = compactable
        self.labels = {button: button.text() for button in compactable}
        self.tooltips = {button: button.toolTip() for button in compactable}
        self.compact = False
        container.installEventFilter(self)

    def full_width(self) -> int:
        """Breite der Knopfleiste mit allen Texten"""
        widths = []
        for i in range(self.layout.count()):
            widget = self.layout.itemAt(i).widget()
            if widget is None or not widget.isVisibleTo(self.container):
                continue
            if widget in self.labels:
                widths.append(max(widget.minimumWidth(),
                                  widget.fontMetrics().horizontalAdvance(self.labels[widget])
                                  + widget.iconSize().width() + 40))
            else:
                widths.append(max(widget.minimumWidth(), widget.sizeHint().width()))
        return sum(widths) + self.layout.spacing() * max(len(widths) - 1, 0)

    def update_mode(self):
        available = self.layout.geometry().width() or self.container.width()
        self.set_compact(self.full_width() > available)

    def set_compact(self, compact: bool):
        if compact == self.compact:
            return
        self.compact = compact
        for button in self.buttons:
            label = self.labels[button].strip()
            button.setText("" if compact else self.labels[button])
            tip = self.tooltips[button]
            button.setToolTip(f"{label} – {tip}" if compact and tip else (label if compact else tip))
            if compact:
                button.setMinimumWidth(0)

    def eventFilter(self, obj, event):
        if obj is self.container and event.type() in (QEvent.Type.Resize, QEvent.Type.Show,
                                                      QEvent.Type.LayoutRequest):
            # Erst nach dem Neu-Layouten messen, sonst stimmt die Breite der Leiste noch nicht
            QTimer.singleShot(0, self.update_mode)
        return False
