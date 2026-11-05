"""
Bereiche eines Bildes von Hand abdecken, bevor es erneut an die KI geht

Rechtecke und Ellipsen liegen in Bildkoordinaten in einer QGraphicsScene. Gezeichnet wird per
Ziehen auf freier Flaeche; Formen lassen sich verschieben und an den Ecken in der Groesse aendern.
"""

from typing import List, Optional, Tuple

from PIL import Image
from PyQt6.QtCore import QPointF, QRectF, QSettings, Qt
from PyQt6.QtGui import QBrush, QColor, QKeySequence, QPen, QPixmap, QShortcut
from PyQt6.QtWidgets import (
    QButtonGroup, QCheckBox, QComboBox, QDialog, QGraphicsItem, QGraphicsPixmapItem, QGraphicsScene, QGraphicsView,
    QHBoxLayout, QLabel, QPushButton, QVBoxLayout,
)

from core.image_masking import EFFECTS, EFFECT_BLUR, MaskShape, SHAPE_ELLIPSE, SHAPE_RECT, apply_masks
from gui import theme
from gui.card_cover_dialog import _select_data, pil_to_pixmap

SETTINGS_EFFECT = "covers/mask_effect"
SETTINGS_SOFT = "covers/mask_soft"
SHAPE_COLOR = QColor(243, 139, 168)


class MaskItem(QGraphicsItem):
    """Eine Form. rect liegt in Bildkoordinaten; das Item selbst wird nie ueber pos() bewegt."""

    def __init__(self, kind: str, rect: QRectF, bounds: QRectF):
        super().__init__()
        self.kind = kind
        self.rect = QRectF(rect)
        self.bounds = bounds
        self.handle = max(bounds.width(), bounds.height()) / 50
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsSelectable)
        self._drag_corner: Optional[int] = None
        self._drag_start = QPointF()
        self._rect_start = QRectF()

    def boundingRect(self) -> QRectF:
        margin = self.handle
        return self.rect.adjusted(-margin, -margin, margin, margin)

    def set_rect(self, rect: QRectF):
        self.prepareGeometryChange()
        self.rect = rect
        self.update()

    def paint(self, painter, option, widget=None):
        pen = QPen(SHAPE_COLOR, 2)
        pen.setCosmetic(True)
        painter.setPen(pen)
        painter.setBrush(QBrush(QColor(SHAPE_COLOR.red(), SHAPE_COLOR.green(), SHAPE_COLOR.blue(), 110)))
        if self.kind == SHAPE_ELLIPSE:
            painter.drawEllipse(self.rect)
        else:
            painter.drawRect(self.rect)
        if self.isSelected():
            painter.setBrush(QBrush(QColor(255, 255, 255)))
            for corner in self._corners():
                painter.drawRect(self._handle_rect(corner))

    def _corners(self) -> List[QPointF]:
        rect = self.rect
        return [rect.topLeft(), rect.topRight(), rect.bottomRight(), rect.bottomLeft()]

    def _handle_rect(self, corner: QPointF) -> QRectF:
        size = self.handle
        return QRectF(corner.x() - size / 2, corner.y() - size / 2, size, size)

    def _corner_at(self, point: QPointF) -> Optional[int]:
        if not self.isSelected():
            return None
        for index, corner in enumerate(self._corners()):
            if self._handle_rect(corner).adjusted(-self.handle / 2, -self.handle / 2,
                                                  self.handle / 2, self.handle / 2).contains(point):
                return index
        return None

    def mousePressEvent(self, event):
        point = event.scenePos()
        self._drag_corner = self._corner_at(point)
        self._drag_start, self._rect_start = point, QRectF(self.rect)
        self.scene().clearSelection()
        self.setSelected(True)
        event.accept()

    def mouseMoveEvent(self, event):
        delta = event.scenePos() - self._drag_start
        if self._drag_corner is None:
            self.set_rect(self._moved(delta))
        else:
            self.set_rect(self._resized(delta))
        event.accept()

    def mouseReleaseEvent(self, event):
        event.accept()

    def _moved(self, delta: QPointF) -> QRectF:
        rect = self._rect_start.translated(delta)
        dx = max(self.bounds.left() - rect.left(), 0) + min(self.bounds.right() - rect.right(), 0)
        dy = max(self.bounds.top() - rect.top(), 0) + min(self.bounds.bottom() - rect.bottom(), 0)
        return rect.translated(dx, dy)

    def _resized(self, delta: QPointF) -> QRectF:
        rect = QRectF(self._rect_start)
        minimum = self.handle
        if self._drag_corner in (0, 3):
            rect.setLeft(min(max(rect.left() + delta.x(), self.bounds.left()), rect.right() - minimum))
        else:
            rect.setRight(max(min(rect.right() + delta.x(), self.bounds.right()), rect.left() + minimum))
        if self._drag_corner in (0, 1):
            rect.setTop(min(max(rect.top() + delta.y(), self.bounds.top()), rect.bottom() - minimum))
        else:
            rect.setBottom(max(min(rect.bottom() + delta.y(), self.bounds.bottom()), rect.top() + minimum))
        return rect


class MaskView(QGraphicsView):
    """Zeichnet neue Formen auf freier Flaeche; Klicks auf vorhandene Formen gehen an die Form"""

    def __init__(self, scene: QGraphicsScene, dialog: "CoverMaskDialog"):
        super().__init__(scene)
        self.dialog = dialog
        self._drawing: Optional[MaskItem] = None
        self._origin = QPointF()
        self._before = None

    def fit(self):
        self.fitInView(self.sceneRect(), Qt.AspectRatioMode.KeepAspectRatio)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.fit()

    def showEvent(self, event):
        super().showEvent(event)
        self.fit()

    def _clamped(self, point: QPointF) -> QPointF:
        bounds = self.sceneRect()
        return QPointF(min(max(point.x(), bounds.left()), bounds.right()),
                       min(max(point.y(), bounds.top()), bounds.bottom()))

    def mousePressEvent(self, event):
        if self.dialog.previewing() or event.button() != Qt.MouseButton.LeftButton:
            return
        self._before = self.dialog.snapshot()
        if isinstance(self.itemAt(event.position().toPoint()), MaskItem):
            super().mousePressEvent(event)
            return
        self.scene().clearSelection()
        self._origin = self._clamped(self.mapToScene(event.position().toPoint()))
        self._drawing = self.dialog.add_item(self.dialog.tool(), QRectF(self._origin, self._origin))

    def mouseMoveEvent(self, event):
        if self._drawing is None:
            super().mouseMoveEvent(event)
            return
        point = self._clamped(self.mapToScene(event.position().toPoint()))
        self._drawing.set_rect(QRectF(self._origin, point).normalized())

    def mouseReleaseEvent(self, event):
        drawing, self._drawing = self._drawing, None
        if drawing is None:
            super().mouseReleaseEvent(event)
        elif min(drawing.rect.width(), drawing.rect.height()) < drawing.handle:
            self.scene().removeItem(drawing)  # nur geklickt, nicht gezogen
        else:
            drawing.setSelected(True)
        self.dialog.commit_change(self._before)
        self._before = None


class CoverMaskDialog(QDialog):
    def __init__(self, image: Image.Image, shapes: List[MaskShape], parent=None):
        super().__init__(parent)
        self.setWindowTitle("Bereiche abdecken")
        self.setWindowFlags(self.windowFlags() | Qt.WindowType.WindowMaximizeButtonHint)
        self.resize(920, 720)
        self.image = image.convert("RGB")
        self._history: List[List[Tuple]] = []
        self._image_pixmap: QPixmap = pil_to_pixmap(self.image)

        layout = QVBoxLayout(self)
        hint = QLabel("Decke Schriftzüge und Gesichter ab: Form wählen und im Bild aufziehen. An Google geht nur das "
                      "abgedeckte Bild. Die App setzt danach dein Original wieder ein, die abgedeckten Stellen bleiben "
                      "auf der Karte also scharf. Ob Google das Bild dann annimmt, ist nicht garantiert.")
        hint.setWordWrap(True)
        layout.addWidget(hint)

        tools = QHBoxLayout()
        self.btn_rect = QPushButton("Rechteck")
        self.btn_ellipse = QPushButton("Kreis")
        group = QButtonGroup(self)
        for button in (self.btn_rect, self.btn_ellipse):
            button.setCheckable(True)
            button.setAutoDefault(False)
            button.setMinimumWidth(96)
            group.addButton(button)
            tools.addWidget(button)
        self.btn_rect.setChecked(True)
        self.effect_combo = QComboBox()
        for label, value in EFFECTS:
            self.effect_combo.addItem(label, value)
        settings = QSettings()
        _select_data(self.effect_combo, settings.value(SETTINGS_EFFECT, EFFECT_BLUR, type=str))
        self.effect_combo.currentIndexChanged.connect(self._refresh_preview)
        tools.addWidget(QLabel("Wirkung:"))
        tools.addWidget(self.effect_combo)
        self.btn_undo = QPushButton("Rückgängig")
        self.btn_undo.setAutoDefault(False)
        self.btn_undo.clicked.connect(self.undo)
        self.btn_clear = QPushButton("Alle entfernen")
        self.btn_clear.setAutoDefault(False)
        self.btn_clear.clicked.connect(self.clear_shapes)
        tools.addWidget(self.btn_undo)
        tools.addWidget(self.btn_clear)
        tools.addStretch(1)
        self.btn_preview = QPushButton("Vorschau")
        self.btn_preview.setCheckable(True)
        self.btn_preview.setAutoDefault(False)
        self.btn_preview.setToolTip("Zeigt genau das Bild, das an Google geht")
        self.btn_preview.toggled.connect(self._refresh_preview)
        tools.addWidget(self.btn_preview)
        layout.addLayout(tools)

        self.scene = QGraphicsScene(self)
        self.pixmap_item = QGraphicsPixmapItem(self._image_pixmap)
        self.scene.addItem(self.pixmap_item)
        self.scene.setSceneRect(QRectF(self._image_pixmap.rect()))
        self.view = MaskView(self.scene, self)
        self.view.setBackgroundBrush(QBrush(QColor(theme.MANTLE)))
        layout.addWidget(self.view, 1)
        for shape in shapes:
            left, top, right, bottom = shape.box(self.image.size)
            self.add_item(shape.kind, QRectF(left, top, right - left, bottom - top))

        bottom_row = QHBoxLayout()
        self.soft_check = QCheckBox("Übergang zum Original weich überblenden")
        self.soft_check.setToolTip("Normalerweise setzt die App das Original mit harter Kante knapp innerhalb des "
                                   "Bildrands ein. Wähle das, falls dabei ein Sprung sichtbar wird.")
        self.soft_check.setChecked(settings.value(SETTINGS_SOFT, False, type=bool))
        bottom_row.addWidget(self.soft_check)
        bottom_row.addStretch(1)
        btn_cancel = QPushButton("Abbrechen")
        btn_cancel.setAutoDefault(False)
        btn_cancel.clicked.connect(self.reject)
        bottom_row.addWidget(btn_cancel)
        self.btn_send = QPushButton("An Google senden (1 Bildaufruf)")
        self.btn_send.setObjectName("primaryButton")
        self.btn_send.setAutoDefault(False)
        self.btn_send.clicked.connect(self._send)
        bottom_row.addWidget(self.btn_send)
        layout.addLayout(bottom_row)

        QShortcut(QKeySequence(Qt.Key.Key_Delete), self, activated=self.delete_selected)
        QShortcut(QKeySequence.StandardKey.Undo, self, activated=self.undo)
        self._update_state()

    # --- Formen -------------------------------------------------------------------------------

    def tool(self) -> str:
        return SHAPE_ELLIPSE if self.btn_ellipse.isChecked() else SHAPE_RECT

    def previewing(self) -> bool:
        return self.btn_preview.isChecked()

    def add_item(self, kind: str, rect: QRectF) -> MaskItem:
        item = MaskItem(kind, rect, self.scene.sceneRect())
        self.scene.addItem(item)
        return item

    def _items(self) -> List[MaskItem]:
        # Einfuegereihenfolge: Rueckgaengig und die Ausgabe bleiben stabil
        return [i for i in self.scene.items(Qt.SortOrder.AscendingOrder) if isinstance(i, MaskItem)]

    def snapshot(self) -> List[Tuple]:
        return [(i.kind, i.rect.x(), i.rect.y(), i.rect.width(), i.rect.height()) for i in self._items()]

    def _restore(self, state: List[Tuple]):
        for item in self._items():
            self.scene.removeItem(item)
        for kind, x, y, width, height in state:
            self.add_item(kind, QRectF(x, y, width, height))
        self._update_state()

    def commit_change(self, before: Optional[List[Tuple]]):
        """Nach einer Mausaktion: Stand fuer Rueckgaengig merken, falls sich etwas geaendert hat"""
        if before is not None and before != self.snapshot():
            self._history.append(before)
        self._update_state()

    def undo(self):
        if self._history and not self.previewing():
            self._restore(self._history.pop())

    def delete_selected(self):
        selected = [i for i in self._items() if i.isSelected()]
        if not selected or self.previewing():
            return
        self._history.append(self.snapshot())
        for item in selected:
            self.scene.removeItem(item)
        self._update_state()

    def clear_shapes(self):
        if self._items() and not self.previewing():
            self._history.append(self.snapshot())
            self._restore([])

    def shapes(self) -> List[MaskShape]:
        width, height = self.image.size
        return [MaskShape(i.kind, i.rect.x() / width, i.rect.y() / height, i.rect.width() / width,
                          i.rect.height() / height) for i in self._items()]

    def effect(self) -> str:
        return self.effect_combo.currentData()

    def soft(self) -> bool:
        return self.soft_check.isChecked()

    # --- Anzeige ------------------------------------------------------------------------------

    def _update_state(self):
        has_shapes = bool(self._items())
        self.btn_send.setEnabled(has_shapes)
        self.btn_clear.setEnabled(has_shapes)
        self.btn_undo.setEnabled(bool(self._history))

    def _refresh_preview(self, *_):
        previewing = self.previewing()
        if previewing:
            self.pixmap_item.setPixmap(pil_to_pixmap(apply_masks(self.image, self.shapes(), self.effect())))
        else:
            self.pixmap_item.setPixmap(self._image_pixmap)
        for item in self._items():
            item.setVisible(not previewing)

    def _send(self):
        settings = QSettings()
        settings.setValue(SETTINGS_EFFECT, self.effect())
        settings.setValue(SETTINGS_SOFT, self.soft())
        self.accept()
