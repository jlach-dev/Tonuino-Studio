# Release-Checkliste Tonuino-Studio

Schritte vor dem ersten öffentlichen Release (2.0.0) und für jedes weitere.

## Einmalig vor dem ersten Release

- [ ] **Lizenz des Originals geklärt**: LICENSE-Datei im Original (donschoof/tonuino-manager) mit Rechteinhaber und Jahr.
- [ ] **LICENSE im Fork** anlegen: MIT-Text mit zwei Copyright-Zeilen – die des Originals und `Copyright (c) 2026 jlach-dev`.
- [ ] **README**: Abschnitt „Lizenz und Herkunft“ mit Verweis auf das Original.
- [x] **Commit-Adressen umgeschrieben** (30.10.2026): alle eigenen Commits tragen `259598630+jlach-dev@users.noreply.github.com`, auf GitHub per --force-with-lease ersetzt.
- [x] **GitHub-Einstellungen** (Settings → Emails): „Keep my email addresses private“ und „Block command line pushes that expose my email“ einschalten.
- [ ] **fpcalc-Prüfsummen** (`FPCALC_SHA256` in `src/core/acoustid.py`) eintragen.
- [ ] **Gemini-Modellnamen** (`src/core/ai_settings.py`) gegen die aktuelle Modellliste prüfen.
- [ ] **Release-Workflow**: Tag `v*` baut alle Plattformen und hängt die Installer an ein GitHub-Release (Dateinamen passend zu `core/updater.py`).
- [ ] **Screenshot** `docs/screenshot.png` erneuern.
- [ ] **Repository öffentlich schalten** – erst ganz am Ende.

## Für jedes Release

- [ ] Original nachziehen (`git fetch upstream` und mergen), Konflikte auflösen.
- [ ] Version in `src/core/__init__.py` erhöhen (2.0.x Korrekturen, 2.x neue Funktionen).
- [ ] `python -m pyflakes src main.py tests` und `python -m pytest -q` grün.
- [ ] Handtest mit der Testkarte: Bereinigen, Import (Ersetzen/Anhängen), Kartencover, Druckbogen, RFID.
- [ ] Windows-Installer neben installiertem Tonuino-Manager testen.
- [ ] Tag `vX.Y.Z` setzen und pushen, Release-Notes schreiben.

## Nach dem Release

- [ ] Allgemeine Fehlerkorrekturen als Pull Requests ans Original.
