# Release-Checkliste Tonuino-Studio

Schritte vor dem ersten öffentlichen Release (2.0.0) und für jedes weitere.

## Einmalig vor dem ersten Release

- [x] **Lizenz des Originals geklärt** (02.11.2026): LICENSE-Datei im Original (donschoof/tonuino-manager) mit Rechteinhaber und Jahr.
- [x] **LICENSE im Fork** angelegt (02.11.2026): MIT-Text mit zwei Copyright-Zeilen – die des Originals und `Copyright (c) 2026 jlach-dev`.
- [x] **README** (02.11.2026): Abschnitt „Lizenz und Herkunft“ mit Verweis auf das Original.
- [x] **Commit-Adressen umgeschrieben** (30.10.2026): alle eigenen Commits tragen `259598630+jlach-dev@users.noreply.github.com`, auf GitHub per --force-with-lease ersetzt.
- [x] **GitHub-Einstellungen** (Settings → Emails): „Keep my email addresses private“ und „Block command line pushes that expose my email“ einschalten.
- [x] **fpcalc-Prüfsummen** (`FPCALC_SHA256` in `src/core/acoustid.py`) eingetragen (30.10.2026).
- [x] **Gemini-Modellnamen** geprüft (30.10.2026): `gemini-nano-banana-2.1` und `gemini-3.5-flash-lite` sind laut ai.google.dev stabil.
- [x] **Release-Workflow** (`.github/workflows/build.yml`): Ein Tag `v*` prüft, dass er zu `__version__` passt, legt einen Release-Entwurf an, baut alle Plattformen und hängt die Installer an. Beim ersten Release einmal genau hinsehen.
- [x] **Screenshot** `docs/screenshot.png` erneuert (30.10.2026, erfundene Hörspiele mit selbst gezeichneten Covern – keine geschützten Bilder).
- [ ] **Repository öffentlich schalten** – erst ganz am Ende.

## Für jedes Release

- [ ] Original nachziehen (`git fetch upstream` und mergen), Konflikte auflösen.
- [ ] Version in `src/core/__init__.py` erhöhen (2.0.x Korrekturen, 2.x neue Funktionen).
- [ ] `python -m pyflakes src main.py tests` und `python -m pytest -q` grün.
- [ ] Handtest mit der Testkarte: Bereinigen, Import (Ersetzen/Anhängen), Kartencover, Druckbogen, RFID.
- [ ] Windows-Installer neben installiertem Tonuino-Manager testen.
- [ ] Tag `vX.Y.Z` setzen und pushen (`git tag v2.0.0` und `git push origin v2.0.0`) – der Workflow legt den Entwurf an.
- [ ] Auf GitHub im Entwurf Release-Notes ergänzen, Installer prüfen, dann „Publish release“. Erst danach sehen Updater und Nutzer die Version.

## Nach dem Release

- [ ] Allgemeine Fehlerkorrekturen als Pull Requests ans Original.
