"""
Erzeugt die Bilder fuer das Handbuch (docs/screenshots/) ohne geschuetzte Inhalte:
echte Oberflaeche, aber erfundene Hoerspiele und selbst gezeichnete Cover.

Arbeitet nur in einem Temp-Ordner mit eigenen Einstellungen, oeffnet keine echte
SD-Karte und sperrt jede Netzwerkverbindung. Aufruf aus dem Projektordner:

    python tools/make_manual_screenshots.py
"""

import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import types
from io import BytesIO
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUTPUT = ROOT / "docs" / "screenshots"
sys.path.insert(0, str(ROOT / "src"))
os.environ["QT_QPA_PLATFORM"] = "offscreen"
if sys.platform == "win32":
    os.environ.setdefault("QT_QPA_FONTDIR", r"C:\Windows\Fonts")


def _no_network(*_args, **_kwargs):
    raise OSError("Beim Erzeugen der Handbuch-Bilder ist kein Netzwerk erlaubt")


socket.socket.connect = _no_network
socket.create_connection = _no_network

import imageio_ffmpeg  # noqa: E402
from mutagen.id3 import APIC, ID3, TALB, TIT2, TPE1, TRCK  # noqa: E402
from PIL import Image, ImageDraw  # noqa: E402
from PyQt6.QtCore import QCoreApplication, QSettings  # noqa: E402
from PyQt6.QtGui import QFontDatabase  # noqa: E402
from PyQt6.QtWidgets import QApplication  # noqa: E402

GREEN = ((58, 110, 76), (160, 205, 110))
BLUE = ((22, 44, 92), (92, 170, 214))
NIGHT = ((16, 18, 52), (64, 52, 120))
ROSE = ((112, 44, 80), (240, 160, 130))
SAND = ((176, 120, 60), (244, 216, 150))
TEAL = ((16, 78, 84), (120, 208, 184))


def background(colors, size=600):
    image = Image.new("RGB", (size, size))
    top, bottom = colors
    draw = ImageDraw.Draw(image)
    for y in range(size):
        draw.line([(0, y), (size, y)], fill=tuple(top[i] + (bottom[i] - top[i]) * y // size for i in range(3)))
    return image, draw


def draw_forest(colors):
    image, draw = background(colors)
    for x in (130, 300, 470):
        draw.rectangle([x - 14, 440, x + 14, 510], fill=(92, 58, 36))
        draw.polygon([(x, 130), (x - 100, 450), (x + 100, 450)], fill=(26, 76, 50))
    return image


def draw_boat(colors):
    image, draw = background(colors)
    draw.ellipse([420, 70, 520, 170], fill=(250, 226, 130))
    draw.rectangle([0, 420, 600, 600], fill=(18, 62, 112))
    draw.polygon([(170, 400), (430, 400), (380, 460), (220, 460)], fill=(120, 66, 40))
    draw.rectangle([294, 170, 306, 400], fill=(70, 46, 30))
    draw.polygon([(306, 180), (306, 380), (440, 380)], fill=(244, 240, 226))
    return image


def draw_moon(colors):
    image, draw = background(colors)
    draw.ellipse([190, 140, 420, 370], fill=(246, 232, 170))
    draw.ellipse([250, 120, 470, 340], fill=colors[0])
    for x, y in ((90, 90), (140, 300), (500, 420), (420, 90), (80, 480), (300, 500), (520, 240)):
        draw.polygon([(x, y - 16), (x + 5, y - 5), (x + 16, y), (x + 5, y + 5), (x, y + 16), (x - 5, y + 5),
                      (x - 16, y), (x - 5, y - 5)], fill=(250, 240, 200))
    return image


def draw_rocket(colors):
    image, draw = background(colors)
    draw.ellipse([60, 420, 240, 600], fill=(190, 96, 70))
    draw.polygon([(300, 90), (250, 220), (350, 220)], fill=(226, 74, 74))
    draw.rectangle([250, 220, 350, 420], fill=(232, 232, 240))
    draw.ellipse([276, 260, 324, 308], fill=(80, 150, 210))
    draw.polygon([(250, 340), (190, 440), (250, 420)], fill=(226, 74, 74))
    draw.polygon([(350, 340), (410, 440), (350, 420)], fill=(226, 74, 74))
    draw.polygon([(265, 420), (335, 420), (300, 530)], fill=(250, 186, 70))
    return image


def draw_owl(colors, body=(120, 84, 56), belly=(226, 196, 150)):
    image, draw = background(colors)
    draw.ellipse([70, 60, 170, 160], fill=(248, 234, 170))
    draw.rectangle([0, 470, 600, 500], fill=(76, 52, 36))
    draw.ellipse([170, 170, 430, 480], fill=body)
    draw.ellipse([225, 290, 375, 465], fill=belly)
    draw.polygon([(190, 215), (215, 130), (270, 185)], fill=body)
    draw.polygon([(410, 215), (385, 130), (330, 185)], fill=body)
    for x in (250, 350):
        draw.ellipse([x - 44, 205, x + 44, 293], fill=(246, 240, 224))
        draw.ellipse([x - 18, 231, x + 18, 267], fill=(36, 28, 24))
    draw.polygon([(285, 280), (315, 280), (300, 315)], fill=(236, 164, 60))
    return image


def draw_notes(colors):
    image, draw = background(colors)
    for x, y in ((190, 360), (400, 310)):
        draw.ellipse([x - 54, y - 40, x + 54, y + 40], fill=(44, 36, 60))
        draw.rectangle([x + 38, y - 220, x + 54, y], fill=(44, 36, 60))
    draw.polygon([(228, 140), (454, 90), (454, 140), (228, 190)], fill=(44, 36, 60))
    return image


def draw_lighthouse(colors):
    image, draw = background(colors)
    draw.rectangle([0, 470, 600, 600], fill=(30, 60, 70))
    draw.polygon([(250, 470), (350, 470), (328, 170), (272, 170)], fill=(240, 240, 232))
    draw.polygon([(261, 310), (339, 310), (333, 240), (267, 240)], fill=(206, 62, 62))
    draw.rectangle([262, 120, 338, 170], fill=(250, 220, 110))
    draw.polygon([(300, 70), (252, 120), (348, 120)], fill=(206, 62, 62))
    draw.polygon([(338, 130), (600, 60), (600, 200)], fill=(250, 226, 150))
    return image


def draw_house(colors):
    image, draw = background(colors)
    draw.rectangle([0, 450, 600, 600], fill=(120, 150, 70))
    draw.rectangle([170, 280, 430, 480], fill=(240, 226, 196))
    draw.polygon([(140, 280), (460, 280), (300, 130)], fill=(160, 70, 54))
    draw.rectangle([270, 380, 330, 480], fill=(100, 66, 44))
    draw.rectangle([195, 320, 245, 370], fill=(130, 190, 220))
    draw.rectangle([355, 320, 405, 370], fill=(130, 190, 220))
    return image


# Alles erfunden: Reihe, Folge, Interpret, Tracktitel
SD_ALBUMS = [
    ("Die Waldwichtel: Folge 1: Das Moosversteck", "Erzählbühne Morgentau", draw_forest, GREEN,
     ["Im Moos", "Der verschwundene Pilz", "Eulen-Post", "Ein Fest im Wald"]),
    ("Kapitän Kuckuck: Die Reise zur Insel", "Erzählbühne Morgentau", draw_boat, BLUE,
     ["Leinen los", "Sturm vor Muschelriff", "Die Flaschenpost", "Land in Sicht"]),
    ("Gute-Nacht-Geschichten: Sterne zählen", "Lesekreis Abendrot", draw_moon, NIGHT,
     ["Der müde Mond", "Sieben Sterne", "Schlaf gut, kleiner Igel"]),
    ("Das Sternenschiff: Abflug zum Mars", "Erzählbühne Morgentau", draw_rocket, NIGHT,
     ["Countdown", "Schwerelos", "Roter Staub", "Heimflug"]),
    ("Lieder für die Reise: Unterwegs", "Die Hummelband", draw_notes, SAND,
     ["Wir fahren los", "Bist du müde, Autobahn", "Regen auf dem Dach"]),
]
LIBRARY_ALBUMS = SD_ALBUMS + [
    ("Der Leuchtturm: Licht im Nebel", "Lesekreis Abendrot", draw_lighthouse, TEAL,
     ["Nebelhorn", "Die Möwe Mathilda", "Sturmnacht"]),
    ("Haus am Hügel: Der Dachboden", "Erzählbühne Morgentau", draw_house, ROSE,
     ["Die alte Truhe", "Das Geheimfach", "Omas Brief"]),
]


def jpeg(image, size=500):
    buffer = BytesIO()
    image.resize((size, size)).save(buffer, "JPEG", quality=88)
    return buffer.getvalue()


def png(image):
    buffer = BytesIO()
    image.save(buffer, "PNG")
    return buffer.getvalue()


def silent_mp3(path, seconds=153):
    subprocess.run([imageio_ffmpeg.get_ffmpeg_exe(), "-y", "-loglevel", "error", "-f", "lavfi", "-i",
                    "anullsrc=r=22050:cl=mono", "-t", str(seconds), "-q:a", "9", str(path)], check=True)


def write_album(folder, album, artist, art, titles, source):
    folder.mkdir(parents=True)
    for number, title in enumerate(titles, start=1):
        path = folder / f"{number:03d}.mp3"
        shutil.copyfile(source, path)
        tags = ID3()
        tags.add(TIT2(encoding=3, text=title))
        tags.add(TALB(encoding=3, text=album))
        tags.add(TPE1(encoding=3, text=artist))
        tags.add(TRCK(encoding=3, text=str(number)))
        tags.add(APIC(encoding=3, mime="image/jpeg", type=3, desc="Cover", data=art))
        tags.save(path)


def settle(app, seconds=0.6, until=None):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        app.processEvents()
        if until is not None and until():
            break
        time.sleep(0.01)
    app.processEvents()


def save(widget, name):
    widget.grab().save(str(OUTPUT / f"{name}.png"))
    print(f"docs/screenshots/{name}.png")


def build_data(base):
    source = base / "stille.mp3"
    silent_mp3(source)
    sd_root, library = base / "sd", base / "Hoerspiele"
    for index, (album, artist, painter, colors, titles) in enumerate(SD_ALBUMS, start=1):
        write_album(sd_root / f"{index:02d}", album, artist, jpeg(painter(colors)), titles, source)
    for album, artist, painter, colors, titles in LIBRARY_ALBUMS:
        write_album(library / album.split(":")[0], album, artist, jpeg(painter(colors)), titles, source)
    (sd_root / "mp3").mkdir()
    (sd_root / "advert").mkdir()
    return sd_root, library, source


def main():
    OUTPUT.mkdir(parents=True, exist_ok=True)
    base = Path(tempfile.mkdtemp(prefix="tonuino_handbuch_"))
    try:
        QSettings.setDefaultFormat(QSettings.Format.IniFormat)
        QSettings.setPath(QSettings.Format.IniFormat, QSettings.Scope.UserScope, str(base / "settings"))
        QCoreApplication.setOrganizationName("TonuinoStudioHandbuch")
        QCoreApplication.setApplicationName("Bilder")
        app = QApplication([])
        QFontDatabase.addApplicationFont(str(ROOT / "src" / "resources" / "fonts" / "MaterialSymbolsOutlined-subset.ttf"))
        render(app, base)
        return 0
    finally:
        shutil.rmtree(base, ignore_errors=True)


def render(app, base):
    from core.library import Library
    from core.tonuino_serial import TonuinoSerial
    from gui import theme

    theme.set_theme("dark")
    from gui.styles import MAIN_STYLESHEET
    app.setStyleSheet(MAIN_STYLESHEET)

    from core import system_files
    from core.cover_archive import CoverArchive
    from core.metadata import MetadataManager
    from core.sd_card import SDCard
    from core.system_files import SystemFile
    from gui import cover_manager_dialog as cm
    from gui.data_folder_dialog import DataFolderDialog
    from gui.main_window import MainWindow
    from gui.print_sheet_dialog import PrintSheetDialog
    from gui.system_files_dialog import SystemFilesDialog

    sd_root, library, _source = build_data(base)
    data_dir = base / "daten"
    settings = QSettings()
    settings.setValue("library/data_dir", str(data_dir))
    settings.setValue("library/sources", [str(library)])
    settings.setValue("library/write_id_tags", "no")
    settings.setValue("updater/auto_check_enabled", False)
    Library(str(data_dir), [str(library)]).save(Library(str(data_dir), [str(library)]).scan())

    TonuinoSerial.list_ports = staticmethod(lambda: [types.SimpleNamespace(device="COM3", description="USB-Seriell")])

    # Hauptfenster: SD-Karte mit geoeffnetem Ordner
    window = MainWindow()
    window.resize(1280, 800)
    window.show()
    settle(app)
    window.sd_card = SDCard(str(sd_root))
    window.sd_card.scan()
    window._open_sd_dialog = types.SimpleNamespace(close=lambda: None)
    window._on_scan_finished(True)
    settle(app, 1.5)
    window.folder_list.setCurrentRow(0)
    window._on_folder_selected(window.folder_list.item(0))
    settle(app, 1.5)
    window.status_bar.showMessage("Bereit")
    settle(app, 0.2)
    save(window, "hauptfenster")

    # Bibliothek
    window.view_switch.setCurrentIndex(1)
    settle(app, 8, until=lambda: window._library_worker is not None and not window._library_worker.isRunning())
    settle(app, 1.0)
    window.status_bar.showMessage("Bereit")
    settle(app, 0.2)
    save(window, "bibliothek")
    window.view_switch.setCurrentIndex(0)
    settle(app, 0.2)

    # Cover-Manager: Galerie und Reiter Karte, ohne Suche und Recherche
    cm.CoverManagerDialog._start_research = lambda self: None
    cm.CoverManagerDialog._start_search = lambda self: None
    folder = window.sd_card.folders[1]
    dialog = cm.CoverManagerDialog(folder, SD_ALBUMS[0][0], MetadataManager(), CoverArchive(str(data_dir)))
    dialog.resize(1280, 820)
    dialog.show()
    settle(app, 0.5)
    variants = [("Eigene Zeichnung: Wald", draw_forest, GREEN), ("Eigene Zeichnung: Abend", draw_forest, NIGHT),
                ("Eigene Zeichnung: Herbst", draw_forest, SAND), ("Eigene Zeichnung: Haus", draw_house, ROSE)]
    first = None
    for label, painter, colors in variants:
        candidate = cm.Candidate(cm.SECTION_ONLINE, label, png(painter(colors)), cm.KIND_ONLINE)
        dialog._add_candidate(candidate, select=first is None)
        first = first or candidate
    settle(app, 0.5)
    dialog._pick_source(back=False)
    settle(app, 0.5)
    dialog.tabs.setCurrentIndex(0)
    settle(app, 0.5)
    save(dialog, "cover-manager")
    dialog.tabs.setCurrentIndex(1)
    settle(app, 0.5)
    save(dialog, "cover-manager-karte")

    # KI erzeugen: alles von Hand ausgefuellt, die Bilder unter "Neu" sind eigene Zeichnungen
    cm.CoverManagerDialog.unsaved_variants = lambda self: []
    dialog.extra_edit.setText("Eule mit Laterne im Moos")
    dialog.research_edit.setPlainText(
        "Die Waldwichtel ist eine Hörspielreihe für Kinder ab vier Jahren. In Folge 1 suchen die Wichtel Moos und Tannzapf einen verschwundenen Pilz und bekommen Hilfe von der Eule Ottilie. Stimmung: warm, freundlich, abendlicher Wald.")
    dialog.sources_label.setText("")
    first.reference = True
    for label, colors, body in (("KI-Variante 1", NIGHT, (120, 84, 56)), ("KI-Variante 2", GREEN, (96, 72, 110)),
                                ("KI-Variante 3", SAND, (92, 110, 84))):
        variant = cm.Candidate(cm.SECTION_NEW, label, png(draw_owl(colors, body)), cm.KIND_AI)
        dialog._add_candidate(variant, select=label.endswith("1"))
    dialog.tabs.setCurrentIndex(2)
    settle(app, 0.5)
    save(dialog, "ki-erzeugen")
    dialog.done(0)
    settle(app, 0.3)

    # Weitere Karten, damit der Druckbogen nicht nur eine zeigt
    for index, (album, _artist, painter, colors, _titles) in list(enumerate(SD_ALBUMS, start=1))[1:3]:
        extra = cm.CoverManagerDialog(window.sd_card.folders[index], album, MetadataManager(), CoverArchive(str(data_dir)))
        extra._add_candidate(cm.Candidate(cm.SECTION_ONLINE, "Eigene Zeichnung", png(painter(colors)), cm.KIND_ONLINE),
                             select=True)
        extra._pick_source(back=False)
        extra.done(0)
        settle(app, 0.2)

    # Druckbogen
    window.sd_card.scan()
    folders = [f for _, f in sorted(window.sd_card.folders.items())]
    names = {f.index: album[0] for f, album in zip(folders, SD_ALBUMS)}
    sheet = PrintSheetDialog(folders, names, window)
    sheet.show()
    settle(app, 0.8)
    save(sheet, "druckbogen")
    sheet.reject()

    # Systemdateien: Release-Liste und Dateien erfunden, damit es ohne Netz geht
    manifest = [SystemFile("mp3", f"{i:04d}.mp3", "0" * 40, 1) for i in range(1, 6)] + [
        SystemFile("advert", "0001.mp3", "0" * 40, 1)]
    system_files.fetch_releases = lambda: ["V3.3.3_2026.02.25", "V3.3.2_2025.12.28"]
    system_files.fetch_manifest = lambda tag, language: manifest
    system = SystemFilesDialog(sd_root, window)
    system.show()
    settle(app, 3, until=lambda: not system._busy() and bool(system.summary_label.text()))
    settle(app, 0.3)
    save(system, "systemdateien")
    system.done(0)

    # Datenordner: Beispielpfad statt des Temp-Ordners
    folder_dialog = DataFolderDialog(window)
    folder_dialog.path_label.setText("Aktuell: D:\\Tonuino\\Bibliothek-Daten")
    folder_dialog.show()
    settle(app, 0.4)
    save(folder_dialog, "datenordner")
    folder_dialog.done(0)

    window.close()
    settle(app, 0.3)


if __name__ == "__main__":
    sys.exit(main())
