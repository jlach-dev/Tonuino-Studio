"""
Gemeinsam genutzte GUI-Helfer (Pfade, Schriften, Dialoge, Icons)
"""

import os
import sys

from PyQt6.QtWidgets import QLabel, QMessageBox
from PyQt6.QtCore import QEvent, QObject, Qt, pyqtSignal
from PyQt6.QtGui import QColor, QFont, QIcon, QPainter, QPixmap
from gui import theme


def resource_path(*parts) -> str:
    """Ermittelt einen Pfad relativ zum src-Verzeichnis - funktioniert sowohl im
    Skript- als auch im PyInstaller-EXE-Modus (siehe auch main.py).
    Im (onefile-)EXE-Modus liegen gebuendelte Daten unter sys._MEIPASS, nicht
    neben der EXE-Datei."""
    if getattr(sys, 'frozen', False):
        base_dir = getattr(sys, '_MEIPASS', os.path.dirname(sys.executable))
        src_dir = os.path.join(base_dir, 'src')
    else:
        src_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(src_dir, *parts)


def heading_font(point_size: int) -> QFont:
    """Fette Ueberschriften-Schriftart. 'Segoe UI' wird nur unter Windows
    explizit gesetzt (dort das native Default) - unter macOS/Linux existiert
    diese Schriftart nicht, ein fest codierter Name fuehrt dort zu einer
    Qt-Log-Warnung ("Populating font family aliases... Replace uses of
    missing font family") und einem uneinheitlichen Fallback-Rendering.
    Ohne explizite Familie verwendet Qt dort automatisch die native
    System-Schriftart (San Francisco bzw. die Desktop-Standardschrift)."""
    font = QFont("Segoe UI") if sys.platform == "win32" else QFont()
    font.setPointSize(point_size)
    font.setWeight(QFont.Weight.Bold)
    return font


class _ProgressLock(QObject):
    """Faengt X und Esc am Fortschrittsfenster ab: Beides wuerde es schliessen und das
    Hauptfenster freigeben, obwohl der Worker noch laeuft. Wo Abbrechen moeglich ist,
    wirken beide wie der Abbrechen-Knopf."""

    def __init__(self, dialog, cancellable: bool):
        super().__init__(dialog)
        self.cancellable = cancellable

    def eventFilter(self, watched, event):
        escape = event.type() == QEvent.Type.KeyPress and event.key() == Qt.Key.Key_Escape
        if event.type() != QEvent.Type.Close and not escape:
            return False
        event.ignore()
        if self.cancellable:
            watched.canceled.emit()
        return True


def lock_progress_dialog(dialog, cancellable: bool = False):
    """Das Fenster bleibt offen, bis close_progress_dialog() es schliesst"""
    dialog._progress_lock = _ProgressLock(dialog, cancellable)
    dialog.installEventFilter(dialog._progress_lock)


def close_progress_dialog(dialog):
    """Schliesst ein Fortschrittsfenster, wenn die Arbeit getan ist. close() loest bei
    QProgressDialog selbst "canceled" aus, das darf den Worker nicht mehr erreichen."""
    lock = getattr(dialog, "_progress_lock", None)
    if lock is not None:
        dialog.removeEventFilter(lock)
        dialog._progress_lock = None
    try:
        dialog.canceled.disconnect()
    except TypeError:
        pass  # nichts verbunden
    dialog.close()


def keep_open_until_done(dialog, on_cancel, message: str = "Wird nach der aktuellen Datei abgebrochen …"):
    """QProgressDialog schliesst sich beim Abbrechen sofort, der Worker kopiert aber noch
    die aktuelle Datei zu Ende. Das Fenster bleibt deshalb gesperrt, bis er fertig ist,
    sonst liesse sich derselbe Ordner in dieser Zeit verschieben oder loeschen. Schliessen
    mit close_progress_dialog()."""
    dialog.canceled.disconnect(dialog.cancel)

    def cancel():
        on_cancel()
        dialog.setLabelText(message)

    dialog.canceled.connect(cancel)
    lock_progress_dialog(dialog, cancellable=True)


def confirm_action(parent, title: str, text: str, default_no: bool = True) -> bool:
    """Zeigt einen Ja/Nein-Bestaetigungsdialog mit deutschen Buttons.
    Qt uebersetzt die Standard-Yes/No-Buttons von QMessageBox.question() nicht
    automatisch ins Deutsche (haengt von geladenen Qt-Uebersetzungsdateien ab),
    daher werden hier explizit deutsch beschriftete Buttons verwendet."""
    box = QMessageBox(parent)
    box.setIcon(QMessageBox.Icon.Question)
    box.setWindowTitle(title)
    box.setText(text)
    btn_yes = box.addButton("Ja", QMessageBox.ButtonRole.YesRole)
    btn_no = box.addButton("Nein", QMessageBox.ButtonRole.NoRole)
    box.setDefaultButton(btn_no if default_no else btn_yes)
    box.exec()
    return box.clickedButton() == btn_yes


class ClickableLabel(QLabel):
    """QLabel, das per Klick ein Signal auslöst (fuer das Cover-Bild als In-Place-Button)"""
    clicked = pyqtSignal()

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit()
        super().mousePressEvent(event)


def icon_from_glyph(glyph: str, color: str = theme.BASE, size: int = 16) -> QIcon:
    """Rendert ein Glyph aus der gebuendelten Material-Icons-Schriftart (Apache-2.0)
    als QIcon, damit es (anders als reiner Text) mit setIcon() auf Buttons benutzt
    werden kann, ohne die restliche Button-Schrift zu beeinflussen. Plattformneutral
    statt der Windows-exklusiven Segoe Fluent Icons."""
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.GlobalColor.transparent)

    painter = QPainter(pixmap)
    font = QFont("Material Symbols Outlined")
    font.setPointSize(int(size * 0.65))
    painter.setFont(font)
    painter.setPen(QColor(color))
    painter.drawText(pixmap.rect(), Qt.AlignmentFlag.AlignCenter, glyph)
    painter.end()

    return QIcon(pixmap)


def icon_with_hover(glyph: str, color: str, hover_color: str, size: int = 16) -> QIcon:
    """Wie icon_from_glyph, zeigt beim Ueberfahren (QIcon.Mode.Active) aber hover_color.
    Noetig, wenn der Button beim Hover seine Hintergrundfarbe aendert und das Symbol
    sonst darin verschwinden wuerde. Deaktiviert zeichnet Qt es automatisch abgeblasst."""
    icon = QIcon()
    normal = icon_from_glyph(glyph, color=color, size=size).pixmap(size, size)
    active = icon_from_glyph(glyph, color=hover_color, size=size).pixmap(size, size)
    icon.addPixmap(normal, QIcon.Mode.Normal)
    icon.addPixmap(active, QIcon.Mode.Active)
    return icon


def configure_multi_directory_dialog(dialog) -> None:
    """Macht aus einem QFileDialog einen Ordner-Dialog mit Mehrfachauswahl.
    Der native Windows-Dialog kann nur einen Ordner liefern; der Qt-eigene
    Dialog erlaubt die Mehrfachauswahl in seiner Liste bzw. seinem Baum."""
    from PyQt6.QtWidgets import QAbstractItemView, QFileDialog, QListView, QTreeView
    dialog.setOption(QFileDialog.Option.DontUseNativeDialog, True)
    dialog.setOption(QFileDialog.Option.ShowDirsOnly, True)
    dialog.setFileMode(QFileDialog.FileMode.Directory)
    for view in dialog.findChildren(QListView) + dialog.findChildren(QTreeView):
        view.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)


def pick_directories(parent, caption: str, start: str = "") -> list:
    """Ordner waehlen - im nativen Dialog des Systems (einer je Aufruf; mehrere
    Ordner kommen per Drag & Drop aus dem Explorer in die Quellenliste). Der
    Qt-eigene Dialog mit Mehrfachauswahl (configure_multi_directory_dialog)
    passt optisch nicht zur Anwendung. Leer bei Abbruch."""
    from PyQt6.QtWidgets import QFileDialog
    path = QFileDialog.getExistingDirectory(parent, caption, start, QFileDialog.Option.ShowDirsOnly)
    return [os.path.normpath(path)] if path and os.path.isdir(path) else []
