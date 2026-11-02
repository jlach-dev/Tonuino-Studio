"""
Tonuino-Studio (Fork von Tonuino-Manager)
Ein hübsches Tool zum Verwalten von Tonuino SD-Karten und RFID-Karten.
"""

import logging
import sys
import os

# PyInstaller-Kompatibilitaet
if getattr(sys, 'frozen', False):
    # Lauft als kompilierte EXE (onefile-Build, siehe Tonuino-Manager.spec).
    # Gebuendelte Daten (datas=[('src', 'src')]) werden zur Laufzeit nach
    # sys._MEIPASS entpackt - NICHT neben die EXE-Datei selbst.
    base_dir = getattr(sys, '_MEIPASS', os.path.dirname(sys.executable))
    src_dir = os.path.join(base_dir, 'src')
else:
    # Lauft als Python-Skript
    script_dir = os.path.dirname(os.path.abspath(__file__))
    src_dir = os.path.join(script_dir, 'src')

# Fuege src zum Python-Pfad hinzu
if src_dir not in sys.path:
    sys.path.insert(0, src_dir)

from PyQt6.QtCore import QSettings
from PyQt6.QtWidgets import QApplication
from PyQt6.QtGui import QIcon, QFontDatabase
from core import __version__
from core.branding import APP_NAME
from core.app_log import setup_logging
# crash_handler liest keine Farben - darf vor set_theme() importiert werden
from gui.crash_handler import install_exception_hooks


def main():
    # Log zusaetzlich in eine rotierende Datei - die EXE hat keine Konsole
    log_file = setup_logging(logging.INFO)
    logging.getLogger(__name__).info("%s %s startet", APP_NAME, __version__)

    # High DPI Skalierung aktivieren
    os.environ["QT_ENABLE_HIGHDPI_SCALING"] = "1"
    os.environ["QT_AUTO_SCREEN_SCALE_FACTOR"] = "1"

    app = QApplication(sys.argv)
    # Interner Name bleibt "Tonuino-Manager" (Ablageort der Einstellungen), siehe core/branding.py
    app.setApplicationName("Tonuino-Manager")
    app.setApplicationVersion(__version__)
    app.setOrganizationName("TonuinoManager")

    # Unbehandelte Ausnahmen (auch in Threads) loggen und melden, statt die
    # App kommentarlos zu beenden
    install_exception_hooks(log_file)

    icon_path = os.path.join(src_dir, "resources", "icon.ico")
    if os.path.exists(icon_path):
        app.setWindowIcon(QIcon(icon_path))

    # Icon-Schriftart bundlen (Material Symbols Outlined, Apache-2.0; Subset mit
    # den verwendeten Icons) statt der
    # Windows-exklusiven Segoe Fluent Icons/Segoe MDL2 Assets zu verwenden -
    # macht die Button-/Status-Icons auf allen Plattformen (Windows/macOS/
    # Linux) gleichermassen sichtbar, siehe gui/main_window.py.
    icon_font_path = os.path.join(src_dir, "resources", "fonts", "MaterialSymbolsOutlined-subset.ttf")
    if os.path.exists(icon_font_path):
        QFontDatabase.addApplicationFont(icon_font_path)

    # Design waehlen, bevor die gui-Module importiert werden (sie lesen Farben schon
    # beim Import), dann Stylesheet laden
    from gui import theme
    theme.set_theme(QSettings().value(theme.SETTINGS_KEY, theme.DEFAULT_THEME, type=str))
    from gui.styles import MAIN_STYLESHEET
    from gui.main_window import MainWindow
    app.setStyleSheet(MAIN_STYLESHEET)

    window = MainWindow()
    window.show()
    
    sys.exit(app.exec())


if __name__ == "__main__":
    main()

