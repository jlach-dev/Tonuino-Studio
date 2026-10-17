# Tonuino-Manager

Ein hübsches Tool zum Verwalten von Tonuino SD-Karten und RFID-Karten für Windows, Linux und macOS.

![Tonuino-Manager](docs/screenshot.png)

## Features

- **SD-Karten-Verwaltung**: Tonuino-Ordnerstruktur wird automatisch erkannt, Ordner anlegen/löschen
- **Tracks**: hinzufügen, löschen, umsortieren – Dateien werden automatisch lückenlos nummeriert (001.mp3, 002.mp3, …). Maximal 255 Tracks pro Ordner
- **Mehrfachauswahl** (wie in Apple Mail): „Auswählen“ blendet Auswahlkreise ein, Löschen und Nach oben/unten wirken dann auf alle markierten Tracks, „Fertig“ kehrt zur Einzelauswahl zurück
- **Player**: Tracks direkt anhören – Symbol beim Überfahren einer Zeile (Play/Pause), Player oben mit Cover, Titel, Interpret und Album, Playbar, Zurück/Weiter, Lautstärke und Stumm-Schalter
- **Audio-Konvertierung**: MP3, WAV, FLAC, OGG, AAC, WMA, M4A, OPUS werden automatisch nach MP3 konvertiert (FFmpeg ist enthalten)
- **Metadaten-Editor**: ID3-Tags und Cover bearbeiten (Cover per Klick, beim Überfahren erscheint „Cover ändern“) oder Tags durch Leeren der Felder entfernen
- **Ordnername & Cover**: aus Album-Tag und eingebettetem Cover des ersten Tracks; das Ordner-Cover lässt sich direkt in der Ordneransicht ändern
- **SD-Karte bereinigen**: entfernt alles, was nicht zur Tonuino-Struktur gehört, und nummeriert die Tracks je Ordner lückenlos neu; Ordnernummern bleiben unverändert, damit RFID-Karten gültig bleiben (mit Sicherheitsabfrage)
- **RFID-Programmierung**: Ordner- und Admin-Karten per ACR122U oder direkt über den TonUINO, mit Live-Status von Reader, Karte und Ordner in der Sidebar (siehe [RFID-Karten](#rfid-karten))
- **Kartencover**: Cover im Scheckkartenformat (85,6 × 54 mm, hoch oder quer) je Ordner – KI-Illustration per Google Gemini („Nano Banana“) auf Basis einer kurzen Internetrecherche, der Tags und des Album-Covers, oder ohne KI als „Ambient Blur“ aus dem Album-Cover. Der Titel wird per Code gesetzt (siehe [Kartencover](#kartencover))
- **Druckbogen**: A4-PDF für Etikettenbögen (z. B. HERMA 5028) oder zum Selbstausschneiden mit Schnittmarken, inkl. Testseite und Druck-Versatz
- **Fenster**: frei in der Größe veränderbar (Mindestgröße 900 × 600), Vollbild mit F11 oder Menü *Ansicht* (Esc beendet es), Maximieren per Doppelklick auf die Titelleiste; Größe und Zustand werden für den nächsten Start gemerkt, ebenso die Größe des Cover-Managers; Seitenleiste, Vorschau/Galerie und die Spalten des Cover-Managers lassen sich per Trennlinie verschieben. Offene Dialoge werden mit dem Hauptfenster geschlossen
- **Updates**: Prüfung beim Start (im Menü „Einstellungen“ abschaltbar oder manuell auslösbar); der Download wird gegen die von GitHub gemeldete SHA-256-Prüfsumme geprüft

## Installation

Fertige Builds auf der [Releases-Seite](https://github.com/donschoof/tonuino-manager/releases/latest) – keine Python-Installation nötig, FFmpeg ist enthalten.

| System | Datei | Hinweise |
| --- | --- | --- |
| Windows | `Tonuino-Manager-<Version>-Setup.exe` | Installer, deinstallierbar über *Apps & Features* |
| macOS (Apple Silicon) | `Tonuino-Manager-<Version>-arm64.dmg` | App nach `/Applications` ziehen, beim ersten Start [Quarantäne entfernen](#hinweis-zur-signatur-macos) |
| macOS (Intel) | `Tonuino-Manager-<Version>-intel.dmg` | wie oben |
| Linux (Debian/Ubuntu) | `tonuino-manager_<Version>_amd64.deb` | `sudo apt install ./tonuino-manager_<Version>_amd64.deb` |

Portable Programmdateien (z. B. für Distributionen ohne `apt`) lassen sich mit dem [lokalen Build](#eigene-builds) erzeugen.

### Hinweis zur Signatur (macOS)

Die App ist nur ad-hoc signiert (kein Apple Developer Account). Gatekeeper meldet bei heruntergeladenen Kopien deshalb „beschädigt“ – die Datei ist in Ordnung. Quarantäne-Flag entfernen, danach startet die App normal:

```bash
xattr -cr /Applications/Tonuino-Manager.app
```

## SD-Karten-Format

```
SD-Karte/
├── 01/ … 99/   → Tonuino-Ordner mit 001.mp3, 002.mp3, …
│               (optional card.png, card_art_raw.png, card.json, card_back.png,
│                card_back_raw.png = Kartencover)
├── mp3/        → Systemordner (Ansagen), wird nicht umsortiert
└── advert/     → Systemordner (Advert), wird nicht umsortiert
```

Anzeigename und Cover eines Ordners kommen aus den ID3-Tags (Album-Tag `TALB` bzw. eingebettetes Cover `APIC` des ersten passenden Tracks), sonst „Ordner NN“ bzw. eine `cover.jpg`/`cover.png`/`folder.jpg`/`folder.png`/`front.jpg` im Ordner. **Umbenennen** (Stift neben dem Titel oder Doppelklick auf den Ordner bzw. Titel) schreibt den neuen Namen als Album-Tag in alle Tracks des Ordners; vorgeschlagen wird die aus den Tracktiteln erkannte Reihe, wenn der Album-Tag nur ein Podcast-Feed ist. Beides lässt sich im Metadaten-Editor setzen; das Cover auch per Klick auf das Cover in der Ordneransicht (gilt dann für alle Tracks des Ordners).

**SD-Karte bereinigen** löscht unwiderruflich alles außer Ordnern `01`–`99`, `mp3`, `advert` sowie allen Dateien in diesen Ordnern, die keine MP3 sind (inkl. macOS-Metadaten wie `.DS_Store`). Ausnahme: die Kartencover-Dateien `card.png`, `card_art_raw.png`, `card.json`, `card_back.png` und `card_back_raw.png` in den Ordnern `01`–`99` bleiben erhalten. Leere Ordner werden entfernt, die Tracks in jedem Ordner lückenlos neu nummeriert; die Ordnernummern bleiben unverändert. Vorher gibt es eine Vorschau und eine Warnung, wenn das Laufwerk nicht wie eine typische SD-Karte aussieht (kein Wechseldatenträger oder über 32 GB).

## RFID-Karten

Unterstützt werden MIFARE Mini, Classic 1K/4K sowie Ultralight/NTAG21x. Der Kartentyp wird automatisch erkannt.

- **Ordnerkarten**: Ordner plus Wiedergabemodus (Hörspiel, Album, Party, Einzelner Track, Hörbuch)
- **Admin-Karten**: öffnen das Admin-Menü am TonUINO

Den Programmierweg wählst du im Menü **Einstellungen** unter **RFID-Leser**:

- **ACR122U** (Standard): USB-Reader, alle Funktionen inkl. Löschen einzelner Karten
- **TonUINO (seriell)**: Karte wird über den per USB verbundenen TonUINO und dessen eigenen Leser geschrieben, kein extra Reader nötig. Voraussetzung ist [TonUINO-TNG](https://github.com/tonuino/TonUINO-TNG) mit `#define SerialInputAsCommand` (`src/constants.hpp`). Der TonUINO muss im Leerlauf oder pausiert sein, die Karte liegt auf seinem Leser. Löschen ist in diesem Modus nicht möglich.

## Kartencover

Ein Klick auf das Cover im Ordnerkopf (oder **Cover-Manager** bzw. Menü **Kartencover**) öffnet den **Cover-Manager**:

- **Galerie** unter der großen Kartenvorschau, filterbar nach *Aktuell* (eingebettetes Album-Cover, Kartenmotiv auf der SD), *Gefunden*, *Archiv* (alle behaltenen Cover inkl. früherer KI-Läufe) und *Neu*.
- **Cover suchen**: Suchbegriff ist der Album-Tag, sonst der von der Recherche ermittelte Titel, sonst der bereinigte erste Tracktitel (nie „Ordner 01“). Quellen: **Deezer** (bis 1000 px), **Spotify** (640 px, mit eigener Client-ID/Secret), **iTunes** (bis 1200 px), **MusicBrainz**/Cover Art Archive und die **Recherche-Quellen** (Vorschaubild der gefundenen Verlags-/Shopseiten bzw. von der Recherche gemeldete Cover-URLs). Gefundene Cover sind urheberrechtlich geschützt – nur für eigene Karten verwenden.
- **Bedienung**: *Klick* auf ein Bild zeigt es groß als Kartenvorschau – dafür gelten **Als Vorderseite**, **Als Rückseite** und **Als Album-Cover verwenden**; gespeichert wird im Tab „Karte“ mit **Übernehmen**. *Haken* markieren mehrere Bilder für die Sammelaktionen **Behalten (n)** (ins Archiv), **Als KI-Vorlage (n)** und **Verwerfen (n)**. Symbole: ★ im Archiv, ○ nicht gespeichert (verschwindet beim Schließen), ◆ KI-Vorlage, ♪ Album-Cover, ▭ Kartencover. Gefundene Cover werden nur gespeichert, wenn du sie behältst oder verwendest; die automatische Suche läuft nur, solange noch keine gefundenen Cover im Archiv liegen.
- **Ersetzen mit Sicherung**: Ist das bisherige Album-Cover bzw. Kartenmotiv noch nicht im Archiv, fragt der Manager vor dem Überschreiben, ob es dort abgelegt werden soll. Nach „Als Album-Cover“ werden Galerie und Ordneransicht sofort aktualisiert.
- **Kartentext**: *Reihe* (klein, z. B. „Die Fuchsbande · Folge 39“) und *Titel* mit einer Zeile je Folge. Das Zahnrad neben „Reihe“ öffnet ein Popup mit zwei Bereichen (Reihe, Titel) für Schriftart, Größe (mm oder automatisch), Fett und Farbe; jede Änderung ist sofort in der Vorschau zu sehen, „Abbrechen“ stellt die Ausgangswerte wieder her. Die Wahl wird je Karte gespeichert und als Vorgabe für die nächste Karte gemerkt. Die Vorschau zeigt die fertige Karte mit Text nur im Reiter „Karte“, in den anderen Reitern das Bild selbst. Beides wird aus den Tags abgeleitet: Tracktitel nach dem Muster „Reihe (1/2): Folgentitel | Feed“ oder „Reihe – Folge 12: Titel“ (typisch für Podcast-Downloads, bei denen der Album-Tag nur den Feed nennt) haben Vorrang; sonst Interpret/Albuminterpret und Album-Tag, wobei Doppelfolgen wie „Fall 77: …/Fall 78: …“ und mehrere Folgen mit verschiedenen Album-Tags erkannt werden und lässt sich bearbeiten bzw. mit „Aus Tags übernehmen“ neu ermitteln.
- **KI erzeugen**: Recherche und Bild-Prompt stützen sich auf die erkannte Reihe und die Folgentitel (Zeile „Grundlage: …“ zeigt, woher sie stammen: Tracktitel, Tags oder per MusicBrainz bestätigte Tags); ein Album-Tag, der nur einen Podcast-Feed nennt, wird dem Modell ausdrücklich als solcher mitgegeben. Nach „Tags von MusicBrainz“ wird die Recherche automatisch wiederholt. Dazu Stil, eigener Wunsch, 1–4 Varianten; alle mit ◆ markierten Bilder gehen als Vorlage mit (bis zu 6). Nicht behaltene neue Varianten werden beim Schließen abgefragt.
- **Rückseite**: Cover aus dem Cover Art Archive bringen ihre Rückseite mit (Badge ⧉ in der Galerie, Umschalter *Vorderseite | Rückseite* unter der Vorschau). Im Tab „Karte“ unter *Rückseite* wählbar: *Keine*, *Original-Rückseite / gewähltes Bild* (ohne Text; quadratische Bilder per Ambient Blur ins Kartenformat) oder *Trackliste auf Bild* (Überschrift und Trackliste aus den Tags, per Code gesetzt). Als Motiv dient die Rückseite des angeklickten Covers, das gerade in der Vorschau gezeigte Bild („Bild in der Vorschau als Rückseite“; für die Vorderseite gibt es dasselbe) oder eine Datei vom PC. Vorder- und Rückseite sind unabhängig voneinander wählbar („Als Vorderseite“ / „Als Rückseite“ unter der Vorschau). Je Seite gibt es die Checkbox „Rand per KI ergänzen“; **Übernehmen** am Ende des Tabs führt die angehakten Randergänzungen gesammelt aus (je 1 Bildaufruf, Ergebnisse als eigene Versionen unter *Neu*, Originale bleiben unverändert) und speichert danach Vorder- und Rückseite auf die SD-Karte. Gespeichert wird sie mit „Übernehmen“ als `card_back.png`/`card_back_raw.png` im Ordner; auf dem Druckbogen folgen Rückseiten als weitere Etiketten nach den Vorderseiten.
- **Archiv**: alle behaltenen Cover liegen im **Datenordner der Bibliothek** (beim ersten Öffnen wählbar, Standard `Google Drive\Tonuino\Bibliothek-Daten`). Auf die SD-Karte kommt nur das aktive Kartencover. Die Zuordnung Ordner ↔ Archiv erfolgt über eine Album-ID, die als Tag `TXXX:TONUINO_ALBUM_ID` in die MP3s auf der SD-Karte geschrieben wird.

**Tags von MusicBrainz** (Menü *Kartencover* oder Knopf im Reiter *Cover suchen*): Für Ordner mit falschen oder nur aus einem Podcast-Feed stammenden Tags (z. B. Album „MausHörspiel lang“, Titel „Die kleine Hexe (1/2): Heia Walpurgisnacht | …“) sucht der Manager passende Releases mit Trackliste bei **MusicBrainz** (Suchbegriff wie bei der Cover-Suche: erkannte Reihe, sonst Album-Tag), als Ersatz bei **Deezer** und **iTunes**. Releases mit passender Trackanzahl stehen vorn. Die Tracks werden über Position und Länge (±3 s) zugeordnet, sonst über die Länge allein oder die Titelähnlichkeit; bei gleicher Anzahl zuletzt nur über die Position (gelb markiert, bitte prüfen). Die Tabelle zeigt alt → neu je Track für Album, Interpret, Albuminterpret, Titel, Tracknummer, Jahr und MusicBrainz-IDs; jede geänderte Zelle hat ein Häkchen, die Häkchen über der Tabelle schalten ganze Spalten, das Häkchen am Track die ganze Zeile. **Übernehmen** schreibt nur die angehakten Felder in die MP3-Dateien des SD-Ordners; die MusicBrainz-IDs landen wie bei Picard als `TXXX`-Tags (`MusicBrainz Album Id`, `MusicBrainz Release Group Id`, `MusicBrainz Release Track Id`, `MusicBrainz Album Artist Id`, `MusicBrainz Artist Id`). Ordnername und Trackliste werden danach aktualisiert.

**Druckbogen**: Karten auswählen, Bogen wählen, ggf. bei einem angebrochenen Bogen weiter unten beginnen, PDF speichern und in **Originalgröße (100 %)** drucken. Die Bogenränder der HERMA 5028 sind nicht offiziell veröffentlicht – vorher die **Testseite** auf Normalpapier drucken, gegen den Bogen halten und ggf. den Druck-Versatz einstellen.

**Gemini-API-Key**: im [Google AI Studio](https://aistudio.google.com/apikey) erstellen und unter *Kartencover → KI & Online-Dienste* eintragen (wird im Schlüsselbund des Betriebssystems gespeichert) oder als Umgebungsvariable `GEMINI_API_KEY` setzen. Die Bildgenerierung ist kostenpflichtig (grob einige Cent pro Bild). Modell-IDs (Standard: `gemini-nano-banana-2.1` für Bilder, `gemini-3.5-flash-lite` für die Recherche) sind dort ebenfalls änderbar. Für Spotify dort Client-ID und Secret einer eigenen App aus dem [Developer Dashboard](https://developer.spotify.com/dashboard) eintragen (oder `SPOTIFY_CLIENT_ID`/`SPOTIFY_CLIENT_SECRET` setzen).

## Entwicklung

Python 3.10+ und ein venv (auf aktuellem Debian/Ubuntu ohnehin Pflicht, siehe [PEP 668](https://peps.python.org/pep-0668/)):

```bash
git clone https://github.com/donschoof/tonuino-manager.git
cd tonuino-manager
python -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate
pip install -r requirements-dev.txt   # nur zum Starten reicht requirements.txt
python main.py
```

`pyscard` wird je nach System beim Installieren gebaut:

- **Windows**: nichts weiter nötig
- **Linux** (Debian/Ubuntu-Namen):
  ```bash
  sudo apt install pcscd libpcsclite1 libpcsclite-dev swig \
    libxcb-cursor0 libxkbcommon-x11-0 libxcb-icccm4 libxcb-image0 \
    libxcb-keysyms1 libxcb-randr0 libxcb-render-util0 libxcb-shape0 \
    libxcb-xinerama0 libegl1 libpulse0
  ```
- **macOS**: Xcode Command Line Tools und `swig` (sonst bricht `pip install` bei `pyscard` ab, während andere Pakete installiert erscheinen):
  ```bash
  xcode-select --install
  brew install swig
  ```

### Eigene Builds

Im aktivierten venv (mit `requirements-dev.txt` installiert, enthält PyInstaller):

```bash
python build_package.py
```

Das Skript erkennt das Betriebssystem und legt das Ergebnis in `dist/` ab. Es muss auf jeder Zielplattform separat laufen. Die Version kommt aus [src/core/\_\_init\_\_.py](src/core/__init__.py).

- **Windows**: `Tonuino-Manager.exe` (portabel) und mit [Inno Setup](https://jrsoftware.org/isdl.php) zusätzlich das Setup (oder `Build_Package.bat`)
- **Linux**: `Tonuino-Manager` (portabel) und `.deb` (benötigt `dpkg-deb`)
- **macOS**: `Tonuino-Manager.app` und `.dmg` (`arm64` oder `intel`, je nach Build-Rechner)

Tests und Lint lokal:

```bash
python -m pytest -q
python -m pyflakes src main.py tests
```

CI führt Lint und Tests aus, baut jeden Push/PR auf allen Plattformen und startet die App headless als Smoketest ([build.yml](.github/workflows/build.yml)).

## Lizenz

MIT License

Die Releases enthalten FFmpeg (über [imageio-ffmpeg](https://github.com/imageio/imageio-ffmpeg)). Der unter Windows gebündelte Build (FFmpeg 7.1, „essentials“ von [gyan.dev](https://www.gyan.dev/ffmpeg/builds/)) ist mit `--enable-gpl --enable-version3` gebaut und steht damit unter der **GPLv3**; Quellcode und Lizenztext gibt es bei [ffmpeg.org](https://ffmpeg.org/download.html) bzw. beim jeweiligen Builder. Die Linux- und macOS-Builds stammen ebenfalls aus imageio-ffmpeg und wurden hier nicht auf ihre Lizenz geprüft. Für das mitgelieferte FFmpeg gelten dessen Lizenzbedingungen, nicht die MIT-Lizenz dieses Projekts.

Enthält [Material Icons](https://github.com/google/material-design-icons) (Apache License 2.0, siehe `src/resources/fonts/MaterialIcons-LICENSE.txt`).
