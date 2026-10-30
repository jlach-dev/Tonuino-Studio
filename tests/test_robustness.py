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


class _NoTags:
    def read_metadata(self, path):
        from types import SimpleNamespace
        return SimpleNamespace(title="", folder_name="")

    def write_metadata(self, path, **kwargs):
        return True


class _CopyOnly:
    def needs_conversion(self, path):
        return False


def _add_tracks(folder, files):
    from gui.workers import TrackAddWorker
    worker = TrackAddWorker(folder, files, _CopyOnly(), _NoTags())
    added, errors = [], []
    worker.track_added.connect(added.append)
    worker.error.connect(lambda name, msg: errors.append(msg))
    worker.run()  # synchron
    return added, errors


def test_track_add_never_overwrites_files_added_outside_the_app(tmp_path):
    from core.sd_card import SDCard
    (tmp_path / "01").mkdir()
    (tmp_path / "01" / "001.mp3").write_bytes(b"eins")
    card = SDCard(str(tmp_path))
    card.scan()
    (tmp_path / "01" / "002.mp3").write_bytes(b"im Explorer kopiert")  # nach dem Scan
    source = tmp_path / "neu.mp3"
    source.write_bytes(b"neu")

    added, errors = _add_tracks(card.folders[1], [str(source)])

    assert not errors and [t.filename for t in added] == ["003.mp3"]
    assert (tmp_path / "01" / "002.mp3").read_bytes() == b"im Explorer kopiert"
    assert (tmp_path / "01" / "003.mp3").read_bytes() == b"neu"


def test_failed_copy_leaves_no_half_track(tmp_path, monkeypatch):
    from core.sd_card import SDCard
    from gui import workers
    (tmp_path / "01").mkdir()
    card = SDCard(str(tmp_path))
    card.scan()
    source = tmp_path / "neu.mp3"
    source.write_bytes(b"neu")

    def broken_copy(src, dst):
        with open(dst, "wb") as handle:
            handle.write(b"halb")
        raise OSError("NAS nicht erreichbar")

    monkeypatch.setattr(workers.shutil, "copy2", broken_copy)
    added, errors = _add_tracks(card.folders[1], [str(source)])

    assert not added and errors and "NAS" in errors[0]
    assert list((tmp_path / "01").iterdir()) == []


def test_guarded_slot_reports_file_errors_and_ignores_extra_signal_args(app, monkeypatch):
    from PyQt6.QtWidgets import QWidget
    from gui import crash_handler

    shown = []
    monkeypatch.setattr(crash_handler.QMessageBox, "warning", lambda parent, title, text: shown.append(title))

    class Dialog(QWidget):
        @crash_handler.guarded_slot("Speichern")
        def save(self):
            raise PermissionError(5, "Zugriff verweigert")

        @crash_handler.guarded_slot("Zählen")
        def count(self, value):
            return value + 1

    dialog = Dialog()
    assert dialog.save(True) is None  # clicked(bool) liefert ein Argument mehr
    assert shown == ["Speichern"]
    assert dialog.count(1, "extra") == 2
