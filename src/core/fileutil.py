"""Hilfen fuer das Schreiben von Dateien"""
import logging
import os
import time

log = logging.getLogger(__name__)

# Wartezeiten zwischen den Versuchen (Sekunden, zusammen unter 3 s)
REPLACE_DELAYS = (0.05, 0.1, 0.2, 0.4, 0.5, 0.5, 0.5, 0.5)


def replace_with_retry(src, dst, delays=REPLACE_DELAYS):
    """os.replace mit Wiederholung: Unter Windows schlaegt das Ersetzen mit
    WinError 5 fehl, solange ein anderer Prozess die Zieldatei kurz offen hat -
    Google Drive for desktop, Virenscanner oder Indexer direkt nach dem
    vorigen Schreiben. Erst wenn alle Versuche scheitern, wird die temporaere
    Quelldatei entfernt und der Fehler weitergereicht."""
    for attempt, delay in enumerate((*delays, None), start=1):
        try:
            os.replace(src, dst)
            if attempt > 1:
                log.info("Ersetzen von %s gelang im %d. Versuch", dst, attempt)
            return
        except PermissionError:
            if delay is None:
                try:
                    os.remove(src)
                except OSError:
                    pass
                raise
            time.sleep(delay)
