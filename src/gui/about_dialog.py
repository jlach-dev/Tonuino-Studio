"""
"Über Tonuino-Studio": Version, Herkunft und Lizenzen der mitgelieferten Teile
"""

from PyQt6.QtWidgets import QMessageBox, QWidget

from core import __version__
from core.branding import APP_NAME, AUTHOR, HOMEPAGE, UPSTREAM_REPO


def about_text(name: str = APP_NAME) -> str:
    # name: im ESPuino-Modus "ESPuino-Studio" (core/app_mode.py), dasselbe Programm
    edition = f"<p>{APP_NAME} im ESPuino-Modus</p>" if name != APP_NAME else ""
    return (
        f"<h3>{name} {__version__}</h3>{edition}"
        f"<p>von {AUTHOR} – <a href=\"{HOMEPAGE}\">{HOMEPAGE}</a></p>"
        f"<p>Basiert auf <a href=\"https://github.com/{UPSTREAM_REPO}\">Tonuino-Manager</a> von donschoof "
        "(MIT-Lizenz) und übernimmt dessen Weiterentwicklung.</p>"
        f"<p>Lizenz: GNU General Public License v3.0, ohne jede Gewährleistung. Quellcode und Lizenztext: "
        f"<a href=\"{HOMEPAGE}\">{HOMEPAGE}</a></p>"
        "<p><b>Mitgelieferte Software</b><br>"
        "FFmpeg (über imageio-ffmpeg) – GPLv3 im Windows-Build, Quellcode bei ffmpeg.org<br>"
        "Material Symbols – Apache License 2.0<br>"
        "PyQt6 – GPL-3.0; mutagen – GPL-2.0 oder neuer; Qt, pyscard – LGPL<br>"
        "Pillow, pyserial, keyring, google-genai – jeweils eigene Lizenzen</p>"
        "<p><b>Online-Dienste</b> (nur wenn genutzt): MusicBrainz und Cover Art Archive, Deezer, iTunes, "
        "Spotify, AcoustID, Google Gemini für KI-Kartencover sowie GitHub für die TonUINO-Ansagen (TonUINO-TNG, GPL-3.0; nicht mitgeliefert, nur auf Wunsch geladen). Gefundene Cover sind urheberrechtlich "
        "geschützt – nur für eigene Karten verwenden.</p>"
    )


def show_about(parent: QWidget, name: str = APP_NAME):
    QMessageBox.about(parent, f"Über {name}", about_text(name))
