"""Datenordner wechseln: Kopieren, Pruefen, Abbruch, Dialog; RFID-Leser bleibt gespeichert"""
import time

import pytest

pytest.importorskip("PyQt6")

from PyQt6.QtCore import QCoreApplication, QSettings
from PyQt6.QtWidgets import QApplication

from core import ai_settings, data_folder as df
from core.data_folder import DataFolderError


def make_data(root):
    (root / "albums" / "a1" / "covers").mkdir(parents=True)
    (root / "albums" / "a1" / "covers" / "c.png").write_bytes(b"bild")
    (root / "albums" / "desktop.ini").write_bytes(b"windows")
    (root / "series").mkdir()
    (root / "tools").mkdir()
    (root / "tools" / "fpcalc.exe").write_bytes(b"exe")
    (root / "library_index.json").write_text("{}", encoding="utf-8")
    (root / "fremd.txt").write_text("gehoert nicht zur App", encoding="utf-8")
    return root


def tree(root):
    return sorted(str(p.relative_to(root)).replace("\\", "/") for p in root.rglob("*") if p.is_file())


def test_copy_takes_app_data_and_leaves_old_folder(tmp_path):
    old, new = make_data(_mk(tmp_path / "alt")), tmp_path / "neu" / "tief"
    before = tree(old)
    assert df.copy_data(str(old), str(new)) is True
    assert tree(new) == ["albums/a1/covers/c.png", "library_index.json", "tools/fpcalc.exe"]
    assert (new / "albums" / "a1" / "covers" / "c.png").read_bytes() == b"bild"
    assert tree(old) == before  # der alte Ordner bleibt
    assert not (new / df.STAGING_NAME).exists()


def _mk(path):
    path.mkdir(parents=True)
    return path


@pytest.mark.parametrize("relative", ["", "albums", "unter/ordner"])
def test_target_must_not_be_the_old_folder_or_inside(tmp_path, relative):
    with pytest.raises(DataFolderError):
        df.validate_target(str(tmp_path), str(tmp_path / relative))


def test_parent_and_other_folders_are_allowed(tmp_path):
    df.validate_target(str(tmp_path / "alt"), str(tmp_path))
    df.validate_target(str(tmp_path / "alt"), str(tmp_path / "alt2"))


def test_no_merge_into_existing_data(tmp_path):
    old, new = make_data(_mk(tmp_path / "alt")), make_data(_mk(tmp_path / "neu"))
    (new / "library_index.json").write_text('{"neu": 1}', encoding="utf-8")
    with pytest.raises(DataFolderError):
        df.copy_data(str(old), str(new))
    assert (new / "library_index.json").read_text(encoding="utf-8") == '{"neu": 1}'


def test_cancel_leaves_no_data_at_target(tmp_path):
    old, new = make_data(_mk(tmp_path / "alt")), tmp_path / "neu"
    calls = []
    assert df.copy_data(str(old), str(new), cancelled=lambda: calls.append(1) or len(calls) > 1) is False
    assert not df.has_data(new) and list(new.iterdir()) == []


def test_differing_copy_is_an_error_and_cleans_up(tmp_path, monkeypatch):
    old, new = make_data(_mk(tmp_path / "alt")), tmp_path / "neu"
    monkeypatch.setattr(df.filecmp, "cmp", lambda *a, **k: False)
    with pytest.raises(DataFolderError, match="weicht"):
        df.copy_data(str(old), str(new))
    assert not df.has_data(new) and list(new.iterdir()) == []


def test_too_little_space_is_refused(tmp_path, monkeypatch):
    old, new = make_data(_mk(tmp_path / "alt")), tmp_path / "neu"
    monkeypatch.setattr(df.shutil, "disk_usage", lambda p: type("U", (), {"free": 1})())
    with pytest.raises(DataFolderError, match="Platz"):
        df.copy_data(str(old), str(new))
    assert not df.has_data(new)


def test_failed_rename_removes_placed_entries(tmp_path, monkeypatch):
    old, new = make_data(_mk(tmp_path / "alt")), tmp_path / "neu"
    real = df.replace_with_retry

    def fail_on_index(src, dst):
        if str(dst).endswith("library_index.json"):
            raise PermissionError("gesperrt")
        real(src, dst)

    monkeypatch.setattr(df, "replace_with_retry", fail_on_index)
    with pytest.raises(PermissionError):
        df.copy_data(str(old), str(new))
    assert not df.has_data(new) and list(new.iterdir()) == []


def test_moved_path(tmp_path):
    old, new = tmp_path / "alt", _mk(tmp_path / "neu" / "tools").parent
    (new / "tools" / "fpcalc.exe").write_bytes(b"exe")
    inside = str(old / "tools" / "fpcalc.exe")
    assert df.moved_path(inside, str(old), str(new)) == str(new / "tools" / "fpcalc.exe")
    assert df.moved_path(str(old / "tools" / "fehlt.exe"), str(old), str(new)) == str(old / "tools" / "fehlt.exe")
    assert df.moved_path(r"C:\anderswo\fpcalc.exe", str(old), str(new)) == r"C:\anderswo\fpcalc.exe"
    assert df.moved_path("", str(old), str(new)) == ""


# --- Dialog ---------------------------------------------------------------------------------

@pytest.fixture(scope="module")
def app():
    QCoreApplication.setOrganizationName("Tonuino-Manager-Tests")
    QCoreApplication.setApplicationName("tests-data-folder")
    application = QApplication.instance() or QApplication([])
    yield application
    QSettings().clear()


@pytest.fixture
def make_dialog(app, monkeypatch):
    from gui import data_folder_dialog as module
    answers = {"confirm": True}
    monkeypatch.setattr(module, "confirm_action", lambda *a, **k: answers["confirm"])
    QSettings().clear()
    created = []

    def create(current=""):
        QSettings().setValue(module.SETTINGS_DATA_DIR, current)
        dialog = module.DataFolderDialog()
        dialog.answers = answers
        created.append(dialog)
        return dialog

    yield create
    for dialog in created:
        settle(app, dialog)
        dialog.done(0)
    QSettings().clear()


def settle(app, dialog):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and dialog._busy():
        app.processEvents()
        time.sleep(0.005)
    assert not dialog._busy()
    for _ in range(5):
        app.processEvents()


def stored_dir():
    return QSettings().value("library/data_dir", "", type=str)


def test_dialog_copies_then_switches_and_updates_fpcalc(app, make_dialog, tmp_path):
    old, new = make_data(_mk(tmp_path / "alt")), tmp_path / "neu"
    QSettings().setValue(ai_settings.SETTINGS_FPCALC, str(old / "tools" / "fpcalc.exe"))
    dialog = make_dialog(str(old))
    QSettings().setValue(ai_settings.SETTINGS_FPCALC, str(old / "tools" / "fpcalc.exe"))
    dialog.use_folder(str(new))
    assert stored_dir() == str(old)  # waehrend des Kopierens gilt noch der alte Ordner
    settle(app, dialog)
    assert stored_dir() == str(new) and dialog.changed
    assert (new / "library_index.json").exists()
    assert QSettings().value(ai_settings.SETTINGS_FPCALC, "", type=str) == str(new / "tools" / "fpcalc.exe")
    assert "kopiert und geprüft" in dialog.status_label.text()


def test_dialog_declined_copy_changes_nothing(app, make_dialog, tmp_path):
    old, new = make_data(_mk(tmp_path / "alt")), tmp_path / "neu"
    dialog = make_dialog(str(old))
    dialog.answers["confirm"] = False
    dialog.use_folder(str(new))
    settle(app, dialog)
    assert stored_dir() == str(old) and not dialog.changed and not new.exists()


def test_dialog_failed_copy_keeps_old_folder(app, make_dialog, tmp_path, monkeypatch):
    old, new = make_data(_mk(tmp_path / "alt")), tmp_path / "neu"
    monkeypatch.setattr(df.filecmp, "cmp", lambda *a, **k: False)
    dialog = make_dialog(str(old))
    dialog.use_folder(str(new))
    settle(app, dialog)
    assert stored_dir() == str(old) and not dialog.changed
    assert "nicht geklappt" in dialog.status_label.text() and dialog.btn_close.isEnabled()


def test_dialog_uses_existing_folder_without_copying(app, make_dialog, tmp_path):
    old, new = make_data(_mk(tmp_path / "alt")), _mk(tmp_path / "neu")
    (new / "library_index.json").write_text('{"neu": 1}', encoding="utf-8")
    dialog = make_dialog(str(old))
    dialog.use_folder(str(new))
    assert stored_dir() == str(new)
    assert (new / "library_index.json").read_text(encoding="utf-8") == '{"neu": 1}'
    assert not (new / "albums").exists()


def test_dialog_unreachable_old_folder_just_switches(app, make_dialog, tmp_path):
    new = tmp_path / "neu"
    dialog = make_dialog(str(tmp_path / "gibt-es-nicht"))
    dialog.use_folder(str(new))
    assert stored_dir() == str(new) and new.is_dir()
    assert "nicht erreichbar" in dialog.status_label.text()


def test_dialog_without_previous_folder_sets_it(app, make_dialog, tmp_path):
    dialog = make_dialog("")
    dialog.use_folder(str(tmp_path / "neu"))
    assert stored_dir() == str(tmp_path / "neu")


def test_dialog_refuses_folder_inside_old(app, make_dialog, tmp_path):
    old = make_data(_mk(tmp_path / "alt"))
    dialog = make_dialog(str(old))
    dialog.use_folder(str(old / "unter"))
    assert stored_dir() == str(old)
    assert "nicht im bisherigen" in dialog.status_label.text()


def test_dialog_close_during_copy_cancels_first(app, make_dialog, tmp_path, monkeypatch):
    old, new = make_data(_mk(tmp_path / "alt")), tmp_path / "neu"
    dialog = make_dialog(str(old))
    monkeypatch.setattr(df, "copy_data", lambda o, n, p, cancelled: wait_for(cancelled))
    dialog.use_folder(str(new))
    dialog.show()
    dialog.done(0)
    assert dialog.isVisible()
    settle(app, dialog)
    assert stored_dir() == str(old) and "Abgebrochen" in dialog.status_label.text()
    dialog.done(0)
    assert not dialog.isVisible()


def wait_for(cancelled):
    deadline = time.monotonic() + 3
    while not cancelled() and time.monotonic() < deadline:
        time.sleep(0.005)
    return False


# --- Hauptfenster ---------------------------------------------------------------------------

def test_reader_mode_survives_restart(app):
    from gui.main_window import MainWindow
    from gui.rfid_panel import RfidPanel
    QSettings().clear()

    window = MainWindow()
    assert window.rfid_panel._reader_mode == RfidPanel.READER_MODE_ACR122U
    window.action_reader_tonuino.trigger()
    assert window.rfid_panel._reader_mode == RfidPanel.READER_MODE_TONUINO
    window.close()

    again = MainWindow()
    assert again.rfid_panel._reader_mode == RfidPanel.READER_MODE_TONUINO
    assert again.action_reader_tonuino.isChecked()
    again.close()
    QSettings().clear()


def test_data_folder_change_waits_for_running_scan(app, monkeypatch):
    from gui import main_window_library as module
    from gui.main_window import MainWindow
    QSettings().clear()
    window = MainWindow()
    opened, infos = [], []
    monkeypatch.setattr(module, "DataFolderDialog", lambda parent: opened.append(1))
    monkeypatch.setattr(module.QMessageBox, "information", lambda *a: infos.append(a[2]))
    window._library_worker = type("W", (), {"isRunning": lambda self: True})()
    window._change_data_folder()
    assert not opened and "gerade eingelesen" in infos[0]
    window._library_worker = None
    window.close()
    QSettings().clear()
