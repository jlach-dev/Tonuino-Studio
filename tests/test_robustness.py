import logging
import threading
import time
import weakref

import pytest

pytest.importorskip("PyQt6")

from PyQt6.QtCore import QThread, pyqtSignal
from PyQt6.QtWidgets import QApplication


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


class BlockingWorker(QThread):
    result = pyqtSignal(str)

    def __init__(self, release: threading.Event):
        super().__init__()
        self.release = release
        self.cancelled = False

    def cancel(self):
        self.cancelled = True

    def run(self):
        self.release.wait(5)
        self.result.emit("spaet")


def test_retired_worker_stays_alive_and_delivers_nothing(app):
    from gui import workers

    release = threading.Event()
    received = []
    worker = BlockingWorker(release)
    worker.result.connect(received.append)
    worker.start()

    workers.retire_worker(worker)
    assert worker.cancelled
    # Nur diesen Worker verfolgen, nicht die Gesamtzahl - ausgemusterte Worker
    # frueherer GUI-Tests koennen waehrenddessen ebenfalls enden
    ref = weakref.ref(worker)
    del worker  # nur noch der Reaper haelt ihn - ohne ihn: "QThread: Destroyed while thread is still running"
    assert ref() is not None and ref() in workers._reaper._workers

    release.set()
    deadline = time.monotonic() + 5
    while ref() is not None and ref() in workers._reaper._workers and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.01)
    app.processEvents()
    assert ref() is None or ref() not in workers._reaper._workers
    assert received == []


def test_retire_finished_or_missing_worker_is_noop(app):
    from gui.workers import retire_worker, retired_worker_count

    before = retired_worker_count()
    retire_worker(None)
    release = threading.Event()
    release.set()
    worker = BlockingWorker(release)
    worker.start()
    worker.wait(5000)
    retire_worker(worker)
    assert retired_worker_count() == before


def test_unhandled_exception_is_logged_and_reported(app, monkeypatch, caplog):
    from gui import crash_handler

    reported = []

    class FakeSignal:
        def emit(self, summary, details):
            reported.append((summary, details))

    class FakeNotifier:
        crashed = FakeSignal()

    monkeypatch.setattr(crash_handler, "_notifier", FakeNotifier())
    try:
        raise ValueError("kaputt")
    except ValueError as e:
        with caplog.at_level(logging.CRITICAL, logger="gui.crash_handler"):
            crash_handler.handle_exception(type(e), e, e.__traceback__, "Worker-1")

    assert reported and reported[0][0] == "ValueError: kaputt"
    assert "Traceback" in reported[0][1] and "kaputt" in reported[0][1]
    assert any("Worker-1" in r.getMessage() and r.exc_info for r in caplog.records)


def test_setup_logging_writes_rotating_log_file(tmp_path):
    from core.app_log import setup_logging

    root = logging.getLogger()
    handlers, level = list(root.handlers), root.level
    try:
        path = setup_logging(logging.INFO, tmp_path)
        logging.getLogger("test").warning("Hallo Log")
        for handler in root.handlers:
            handler.flush()
        assert path == tmp_path / "tonuino-studio.log"
        assert "Hallo Log" in path.read_text(encoding="utf-8")
    finally:
        for handler in root.handlers[:]:
            if handler not in handlers:
                root.removeHandler(handler)
                handler.close()
        root.setLevel(level)
