"""
Zeile mit malendem Pinsel: zeigt, dass im Hintergrund noch etwas laeuft

Die KI meldet keinen Fortschritt, deshalb gibt es keinen Prozentwert, nur Text und verstrichene
Zeit. Im Ruhezustand ist die Zeile ausgeblendet. Kurze Vorgaenge erscheinen gar nicht erst.
"""

import math
import time
from typing import List, Optional

from PyQt6.QtCore import QObject, QPointF, Qt, QTimer, pyqtSlot
from PyQt6.QtGui import QColor, QPainter, QPainterPath, QPen
from PyQt6.QtWidgets import QFrame, QHBoxLayout, QLabel, QSizePolicy, QWidget

from gui import theme

SHOW_DELAY = 0.4  # Sekunden: Kuerzeres wuerde nur flackern
DONE_SECONDS = 1.5
TICK_MS = 33
CYCLE_SECONDS = 3.0
DRAW_SHARE = 0.85  # danach steht die Linie kurz, bevor sie neu beginnt

BRUSH_LENGTH = 36  # Griff bis Spitze, in Pixeln
BRUSH_TILT = 104  # Grad im Uhrzeigersinn: Die Spitze zeigt nach unten auf die Linie, der Griff steht leicht schraeg


class BrushStroke(QWidget):
    """Zeichnet die Wellenlinie und den Pinsel an ihrem Ende. progress: 0 bis 1, None: ohne Pinsel."""

    WIDTH, HEIGHT = 240, 52  # hoch genug fuer den stehenden Pinsel
    AMPLITUDE = 5
    WAVE = 72

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedSize(self.WIDTH, self.HEIGHT)
        self.progress: Optional[float] = 0.0
        self.color = QColor(theme.BLUE)

    def set_state(self, progress: Optional[float], color: str):
        self.progress = progress
        self.color = QColor(color)
        self.update()

    def line_point(self, share: float) -> QPointF:
        start, end = 6, self.WIDTH - 14  # rechts bleibt Platz fuer den schraegen Griff
        x = start + (end - start) * share
        # Unten in der Flaeche: Der Griff ragt nach oben
        return QPointF(x, self.HEIGHT - 9 + self.AMPLITUDE * math.sin(x * 2 * math.pi / self.WAVE))

    def paintEvent(self, event):
        share = 1.0 if self.progress is None else self.progress
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        path = QPainterPath(self.line_point(0))
        for step in range(1, max(2, round(share * 120)) + 1):
            path.lineTo(self.line_point(share * step / max(2, round(share * 120))))
        painter.setPen(QPen(self.color, 4, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        painter.drawPath(path)
        if self.progress is not None:
            self._paint_brush(painter, self.line_point(share))
        painter.end()

    def _paint_brush(self, painter: QPainter, tip: QPointF):
        """Spitze im Ursprung; vor dem Drehen laeuft der Griff nach links"""
        painter.save()
        painter.translate(tip)
        painter.rotate(BRUSH_TILT)
        painter.setPen(Qt.PenStyle.NoPen)
        bristle = QPainterPath(QPointF(0, 0))
        bristle.cubicTo(QPointF(-3, -3.5), QPointF(-7, -4), QPointF(-11, -3.5))
        bristle.lineTo(QPointF(-11, 3.5))
        bristle.cubicTo(QPointF(-7, 4), QPointF(-3, 3.5), QPointF(0, 0))
        painter.setBrush(self.color)
        painter.drawPath(bristle)
        painter.setBrush(QColor(theme.SUBTEXT0))
        painter.drawRect(-16, -3, 5, 6)
        painter.setBrush(QColor(theme.YELLOW))
        handle = QPainterPath(QPointF(-16, -2.5))
        handle.lineTo(QPointF(-BRUSH_LENGTH, -1))
        handle.lineTo(QPointF(-BRUSH_LENGTH, 1))
        handle.lineTo(QPointF(-16, 2.5))
        handle.closeSubpath()
        painter.drawPath(handle)
        painter.restore()


class _Task(QObject):
    """Empfaenger fuer die Signale eines Workers. Als QObject wird die Verbindung beim Schliessen getrennt."""

    def __init__(self, bar: "ActivityBar", text: str):
        super().__init__(bar)
        self.bar, self.text, self.started = bar, text, bar.now()

    @pyqtSlot()
    def finish(self):
        self.bar.task_finished(self)

    @pyqtSlot()
    @pyqtSlot(str)
    def fail(self, _message: str = ""):
        self.bar.task_failed()


class ActivityBar(QFrame):
    """Laufende Vorgaenge eines Fensters. Vor worker.start(): bar.track(worker, "Text")."""

    now = staticmethod(time.monotonic)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("activityBar")
        self.setStyleSheet(f"#activityBar {{ background: {theme.SURFACE0}; border-radius: 6px; }}")
        layout = QHBoxLayout(self)
        layout.setContentsMargins(8, 2, 10, 2)
        layout.setSpacing(10)
        self.stroke = BrushStroke()
        self.label = QLabel()
        self.label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.seconds = QLabel()
        self.seconds.setStyleSheet(f"color: {theme.SUBTEXT0};")
        layout.addWidget(self.stroke)
        layout.addWidget(self.label, 1)
        layout.addWidget(self.seconds)
        self.tasks: List[_Task] = []
        self._failed = False
        self._timer = QTimer(self)
        self._timer.setInterval(TICK_MS)
        self._timer.timeout.connect(self.refresh)
        self._done_timer = QTimer(self)
        self._done_timer.setSingleShot(True)
        self._done_timer.setInterval(round(DONE_SECONDS * 1000))
        self._done_timer.timeout.connect(self.hide)
        self.hide()

    def track(self, worker, text: str):
        task = _Task(self, text)
        worker.finished.connect(task.finish)
        for name in ("failed", "refused"):  # ein Fehler ist kein "Fertig"
            signal = getattr(worker, name, None)
            if signal is not None:
                signal.connect(task.fail)
        if not self.tasks:
            self._failed = False
        self.tasks.append(task)
        self._done_timer.stop()
        self._timer.start()
        self.refresh()

    def task_failed(self):
        self._failed = True

    def task_finished(self, task: _Task):
        if task not in self.tasks:
            return
        self.tasks.remove(task)
        task.deleteLater()
        elapsed_from = task.started
        if self.tasks:
            self.refresh()
            return
        self._timer.stop()
        if self.isHidden() or self._failed:
            self.hide()
            return
        self.stroke.set_state(None, theme.GREEN)
        self.label.setStyleSheet(f"color: {theme.GREEN};")
        self.label.setText("Fertig")
        self.seconds.setText(f"{self.now() - elapsed_from:.0f} s")
        self._done_timer.start()

    def refresh(self):
        if not self.tasks:
            return
        elapsed = self.now() - min(t.started for t in self.tasks)
        if elapsed < SHOW_DELAY and self.isHidden():
            return
        self.setVisible(True)
        phase = (elapsed % CYCLE_SECONDS) / CYCLE_SECONDS
        self.stroke.set_state(min(1.0, phase / DRAW_SHARE), theme.BLUE)
        texts = list(dict.fromkeys(t.text for t in self.tasks))
        self.label.setStyleSheet("")
        self.label.setText(" · ".join(texts) + " …")
        self.label.setToolTip(self.label.text())
        self.seconds.setText(f"{elapsed:.0f} s")
