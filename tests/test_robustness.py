import logging
import os
import threading
import time
import weakref
from types import SimpleNamespace

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


def test_tonuino_write_buttons_stay_disabled_while_the_box_waits(app, monkeypatch):
    from gui import rfid_panel

    started = []

    class FakeWorker:
        def __init__(self, *args):
            self.completed = SimpleNamespace(connect=lambda slot: None)

        def start(self):
            started.append(True)

        def isRunning(self):
            return False

    monkeypatch.setattr(rfid_panel, "TonuinoCardWorker", FakeWorker)
    monkeypatch.setattr(rfid_panel.QMessageBox, "information", lambda *a, **k: None)
    panel = rfid_panel.RfidPanel(get_folder=lambda: None, folder_name=lambda folder: "", show_status=lambda text: None)
    try:
        monkeypatch.setattr(type(panel.tonuino_serial), "is_connected", property(lambda self: True))
        panel._reader_mode = panel.READER_MODE_TONUINO

        panel._start_tonuino_write(1, 3, None, None)
        panel._update_program_buttons()  # z.B. weil der Nutzer einen anderen Ordner anklickt
        assert not panel.btn_program_card.isEnabled() and not panel.btn_program_admin.isEnabled()
        panel._start_tonuino_write(1, 4, None, None)
        assert len(started) == 1  # kein zweiter Auftrag

        panel._on_tonuino_write_finished(True, "fertig")
        assert panel.btn_program_card.isEnabled()
    finally:
        panel.shutdown()


def test_license_files_are_bundled_and_keep_the_original_notice():
    from pathlib import Path
    root = Path(__file__).resolve().parent.parent
    assert "GNU GENERAL PUBLIC LICENSE" in (root / "LICENSE").read_text(encoding="utf-8")
    assert "David Schäfer" in (root / "LICENSE-MIT-tonuino-manager.txt").read_text(encoding="utf-8")
    spec = (root / "Tonuino-Manager.spec").read_text(encoding="utf-8")
    assert "('LICENSE', '.')" in spec and "('LICENSE-MIT-tonuino-manager.txt', '.')" in spec


def test_about_text_names_fork_author_and_original():
    from core import __version__
    from gui.about_dialog import about_text
    text = about_text()
    assert "Tonuino-Studio" in text and __version__ in text
    assert "donschoof/tonuino-manager" in text and "GPLv3" in text


def test_header_buttons_shrink_to_icons_when_space_is_short(app):
    from PyQt6.QtWidgets import QHBoxLayout, QPushButton, QWidget
    from gui.compact_buttons import CompactButtons

    container = QWidget()
    row = QHBoxLayout(container)
    buttons = [QPushButton(" Tracks hinzufügen"), QPushButton(" Lautstärke angleichen"), QPushButton(" Cover-Manager")]
    for button in buttons:
        row.addWidget(button)
    compact = CompactButtons(container, row, buttons[1:])
    container.show()

    container.resize(300, 50)
    app.processEvents()
    compact.update_mode()
    assert compact.compact and buttons[1].text() == "" and "Lautstärke" in buttons[1].toolTip()
    assert buttons[0].text() == " Tracks hinzufügen"

    container.resize(1200, 50)
    app.processEvents()
    compact.update_mode()
    assert not compact.compact and buttons[2].text() == " Cover-Manager"
    container.close()


def test_header_buttons_second_stage_only_when_needed(app):
    from PyQt6.QtWidgets import QHBoxLayout, QPushButton, QWidget
    from gui.compact_buttons import CompactButtons

    container = QWidget()
    row = QHBoxLayout(container)
    first, second = QPushButton(" Tracks hinzufügen"), QPushButton(" Lautstärke angleichen")
    row.addWidget(first)
    row.addWidget(second)
    compact = CompactButtons(container, row, [second], later=[first])
    container.show()
    icons_only, one_icon = compact.full_width([first, second]), compact.full_width([second])

    container.resize(one_icon + 60, 50)
    app.processEvents()
    compact.update_mode()
    assert compact.level == 1 and first.text() == " Tracks hinzufügen" and second.text() == ""

    container.resize(icons_only + 40, 50)
    app.processEvents()
    compact.update_mode()
    assert compact.level == 2 and first.text() == "" and "Tracks" in first.toolTip()

    container.resize(1200, 50)
    app.processEvents()
    compact.update_mode()
    assert compact.level == 0 and first.text() == " Tracks hinzufügen" and first.toolTip() == ""
    container.close()


def test_replace_with_retry_waits_for_locked_target(tmp_path, monkeypatch):
    """WinError 5: Google Drive/Virenscanner halten die Zieldatei kurz offen"""
    from core import fileutil

    src, dst = tmp_path / "covers.json.tmp", tmp_path / "covers.json"
    src.write_text("neu", encoding="utf-8")
    dst.write_text("alt", encoding="utf-8")
    real_replace, calls = os.replace, []

    def flaky(a, b):
        calls.append(a)
        if len(calls) <= 2:
            raise PermissionError(5, "Zugriff verweigert")
        real_replace(a, b)

    monkeypatch.setattr(fileutil.os, "replace", flaky)
    fileutil.replace_with_retry(src, dst, delays=(0, 0, 0))
    assert len(calls) == 3 and dst.read_text(encoding="utf-8") == "neu" and not src.exists()

    # bleibt gesperrt: Fehler weiterreichen, Ziel unveraendert, keine .tmp-Leiche
    src.write_text("neuer", encoding="utf-8")
    monkeypatch.setattr(fileutil.os, "replace", lambda a, b: (_ for _ in ()).throw(PermissionError(5, "gesperrt")))
    with pytest.raises(PermissionError):
        fileutil.replace_with_retry(src, dst, delays=(0, 0))
    assert dst.read_text(encoding="utf-8") == "neu" and not src.exists()


def test_archive_write_retries_locked_covers_json(tmp_path, monkeypatch):
    from core import fileutil
    from core.cover_archive import KIND_FILE, ROLE_CARD_COVER, CoverArchive
    from tests.test_library import png_bytes

    archive = CoverArchive(str(tmp_path / "data"))
    archive.ensure_album("a1", "Hexe", "")
    entry = archive.add_cover("a1", png_bytes(), KIND_FILE, source="test")
    real_replace, failures = os.replace, [2]

    def flaky(a, b):
        if failures[0]:
            failures[0] -= 1
            raise PermissionError(5, "Zugriff verweigert")
        real_replace(a, b)

    monkeypatch.setattr(fileutil.os, "replace", flaky)
    monkeypatch.setattr(fileutil.time, "sleep", lambda _s: None)
    archive.set_role("a1", entry.id, ROLE_CARD_COVER)
    assert ROLE_CARD_COVER in archive.covers("a1")[0].roles
