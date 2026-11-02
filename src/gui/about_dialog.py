"""
"Über Tonuino-Studio": Version, Herkunft und Lizenzen der mitgelieferten Teile
"""

from PyQt6.QtWidgets import QMessageBox, QWidget

from core import __version__
from core.branding import APP_NAME, AUTHOR, HOMEPAGE, UPSTREAM_REPO


def about_text() -> str:
    return (
        f"<h3>{APP_NAME} {__version__}</h3>"
        f"<p>von {AUTHOR} – <a href=\"{HOMEPAGE}\">{HOMEPAGE}</a></p>"
        f"<p>Basiert auf <a href=\"https://github.com/{UPSTREAM_REPO}\">Tonuino-Manager</a> von donschoof "
        "(MIT-Lizenz) und übernimmt dessen Weiterentwicklung.</p>"
        "<p><b>Mitgelieferte Software</b><br>"
        "FFmpeg (über imageio-ffmpeg) – GPLv3 im Windows-Build, Quellcode bei ffmpeg.org<br>"
        "Material Symbols – Apache License 2.0<br>"
        "Qt / PyQt6, Pillow, mutagen, pyscard, pyserial, keyring, google-genai – jeweils eigene Lizenzen</p>"
        "<p><b>Online-Dienste</b> (nur wenn genutzt): MusicBrainz und Cover Art Archive, Deezer, iTunes, "
        "Spotify, AcoustID, Google Gemini für KI-Kartencover sowie GitHub für die TonUINO-Ansagen (TonUINO-TNG, GPL-3.0; nicht mitgeliefert, nur auf Wunsch geladen). Gefundene Cover sind urheberrechtlich "
        "geschützt – nur für eigene Karten verwenden.</p>"
    )


def show_about(parent: QWidget):
    QMessageBox.about(parent, f"Über {APP_NAME}", about_text())
