"""TonUINO-Systemdateien: Versionserkennung, Vergleich per Pruefsumme, Schreiben, Dialog"""
import json
import sys
import time
import types

import pytest

pytest.importorskip("PyQt6")

from PyQt6.QtCore import QCoreApplication
from PyQt6.QtWidgets import QApplication

from core import system_files as sf
from core.system_files import FirmwareVersion, SystemFile

TAG = "V3.3.3_2026.02.25"
FILES = {"sd-card/mp3/0001.mp3": b"eins", "sd-card/mp3/0508_quiz_game_buzzer_vol+.mp3": b"plus",
         "sd-card/advert/0001.mp3": b"werbung", "sd-card-english/mp3/0001.mp3": b"one"}


def entry(path, data):
    return {"path": path, "type": "blob", "sha": sf.git_blob_sha(data), "size": len(data)}


@pytest.fixture
def github(monkeypatch):
    """Ersetzt GitHub; requests haelt die abgerufenen Adressen fest"""
    requests = []
    other = [{"path": "src/main.cpp", "type": "blob", "sha": "x", "size": 1},
             {"path": "sd-card/mp3", "type": "tree", "sha": "x"},
             {"path": "sd-card/mp3/notizen.txt", "type": "blob", "sha": "x", "size": 1},
             {"path": "sd-card/mp3/../../boese.mp3", "type": "blob", "sha": "x", "size": 1}]
    bad = {}

    def fake_get(url, max_bytes=0):
        requests.append(url)
        if url.endswith("/releases?per_page=100"):
            return json.dumps([{"tag_name": TAG}, {"tag_name": "V3.3.2_2025.12.28"},
                               {"tag_name": "V9", "draft": True}]).encode()
        if "/git/trees/" in url:
            return json.dumps({"tree": other + [entry(p, d) for p, d in FILES.items()]}).encode()
        for path, data in FILES.items():
            if url.replace("%2B", "+").endswith(path):
                return bad.get(path, data)
        raise AssertionError(url)

    monkeypatch.setattr(sf, "_get", fake_get)
    return types.SimpleNamespace(requests=requests, bad=bad)


@pytest.mark.parametrize("line, expected", [
    ("V3.3.3 25.02.26\r\n", FirmwareVersion("3.3.3", "2026.02.25")),
    ("TonUINO Version 3.x - refactored by Boerge1", None),
    ("", None),
    ("mp3 V3.3.3 25.02.26", None),
])
def test_parse_firmware_line(line, expected):
    assert sf.parse_firmware_line(line) == expected


def test_match_release_exact_same_number_and_none():
    tags = [TAG, "V3.3.2_2025.12.28", "V3.3.2_2025.11.15"]
    assert sf.match_release(tags, FirmwareVersion("3.3.3", "2026.02.25")) == (TAG, True)
    assert sf.match_release(tags, FirmwareVersion("3.3.2", "2025.12.19")) == ("V3.3.2_2025.12.28", False)
    assert sf.match_release(tags, FirmwareVersion("1.0.0", "2020.01.01")) == (None, False)


def test_manifest_keeps_special_names_and_skips_the_rest(github):
    files = sf.fetch_manifest(TAG, "sd-card")
    assert sorted((f.folder, f.name) for f in files) == [
        ("advert", "0001.mp3"), ("mp3", "0001.mp3"), ("mp3", "0508_quiz_game_buzzer_vol+.mp3")]


def test_manifest_without_files_is_an_error(github):
    with pytest.raises(sf.SystemFilesError):
        sf.fetch_manifest(TAG, "sd-card-french")


def test_empty_card_gets_all_files(github, tmp_path):
    manifest = sf.fetch_manifest(TAG, "sd-card")
    plan = sf.compare(tmp_path, manifest)
    assert len(plan.missing) == 3 and plan.same == 0
    assert list(tmp_path.iterdir()) == []  # vergleichen schreibt nichts

    assert sf.write_files(tmp_path, TAG, "sd-card", plan.missing) == 3
    assert (tmp_path / "mp3" / "0508_quiz_game_buzzer_vol+.mp3").read_bytes() == b"plus"
    assert (tmp_path / "advert" / "0001.mp3").read_bytes() == b"werbung"
    assert not list(tmp_path.rglob(".tmp_*"))
    again = sf.compare(tmp_path, manifest)
    assert not again.missing and not again.changed and again.same == 3


def test_existing_card_keeps_extra_files_and_folder_spelling(github, tmp_path):
    (tmp_path / "MP3").mkdir()
    (tmp_path / "MP3" / "0001.MP3").write_bytes(b"alt")
    (tmp_path / "MP3" / "9999_eigene.mp3").write_bytes(b"meins")
    plan = sf.compare(tmp_path, sf.fetch_manifest(TAG, "sd-card"))
    assert [f.name for f in plan.changed] == ["0001.mp3"]
    assert plan.only_on_card == ["mp3/9999_eigene.mp3"]

    sf.write_files(tmp_path, TAG, "sd-card", plan.missing + plan.changed)
    names = sorted(p.name for p in (tmp_path / "MP3").iterdir())
    assert names == ["0001.MP3", "0508_quiz_game_buzzer_vol+.mp3", "9999_eigene.mp3"]
    assert (tmp_path / "MP3" / "0001.MP3").read_bytes() == b"eins"
    assert (tmp_path / "MP3" / "9999_eigene.mp3").read_bytes() == b"meins"


def test_wrong_checksum_is_not_written(github, tmp_path):
    github.bad["sd-card/mp3/0001.mp3"] = b"manipuliert"
    item = SystemFile("mp3", "0001.mp3", sf.git_blob_sha(b"eins"), 4)
    with pytest.raises(sf.SystemFilesError):
        sf.write_files(tmp_path, TAG, "sd-card", [item])
    assert not (tmp_path / "mp3" / "0001.mp3").exists()


def test_cancel_keeps_finished_files(github, tmp_path):
    files = sf.fetch_manifest(TAG, "sd-card")
    calls = []
    written = sf.write_files(tmp_path, TAG, "sd-card", files, cancelled=lambda: calls.append(1) or len(calls) > 1)
    assert written == 1


def fake_serial(monkeypatch, lines=None, error=None):
    module = types.ModuleType("serial")
    module.SerialException = type("SerialException", (Exception,), {})
    remaining = list(lines or [])

    class Serial:
        def __init__(self, *args, **kwargs):
            if error:
                raise module.SerialException(error)

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def readline(self):
            return remaining.pop(0) if remaining else b""

    module.Serial = Serial
    monkeypatch.setitem(sys.modules, "serial", module)


def test_detect_reads_version_from_boot_output(monkeypatch):
    fake_serial(monkeypatch, [b"TonUINO Version 3.x - refactored by Boerge1\r\n", b"\xff\r\n", b"V3.3.3 25.02.26\r\n"])
    assert sf.detect_firmware("COM1", timeout=1) == FirmwareVersion("3.3.3", "2026.02.25")


def test_detect_without_version_returns_none(monkeypatch):
    fake_serial(monkeypatch)
    assert sf.detect_firmware("COM1", timeout=0.01) is None


def test_detect_busy_port_gives_a_clear_error(monkeypatch):
    fake_serial(monkeypatch, error="Zugriff verweigert")
    with pytest.raises(sf.SystemFilesError, match="trennen"):
        sf.detect_firmware("COM1", timeout=0.01)


# --- Dialog ---------------------------------------------------------------------------------

@pytest.fixture(scope="module")
def app():
    QCoreApplication.setOrganizationName("Tonuino-Manager-Tests")
    QCoreApplication.setApplicationName("tests-system-files")
    return QApplication.instance() or QApplication([])


def settle(app, dialog):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        app.processEvents()
        if not dialog._busy():
            app.processEvents()
            if not dialog._busy():
                return
        time.sleep(0.005)
    raise AssertionError("Hintergrundaufgabe wurde nicht fertig")


@pytest.fixture
def dialog(app, github, tmp_path, monkeypatch):
    from gui import system_files_dialog as module
    monkeypatch.setattr(module.TonuinoSerial, "list_ports", staticmethod(lambda: [
        types.SimpleNamespace(device="COM1", description="Arduino")]))
    instance = module.SystemFilesDialog(tmp_path)
    settle(app, instance)
    yield instance
    instance.done(0)


def test_dialog_compares_newest_release_and_writes_empty_card(app, dialog, tmp_path):
    assert dialog.release_combo.currentText() == TAG
    assert dialog.release_combo.count() == 2  # Entwurf nicht
    assert "Fehlt: 3" in dialog.summary_label.text()
    assert dialog.btn_write.isEnabled()

    dialog.btn_write.click()
    settle(app, dialog)
    settle(app, dialog)
    assert (tmp_path / "mp3" / "0001.mp3").read_bytes() == b"eins"
    assert dialog.card_changed
    assert "Identisch: 3" in dialog.summary_label.text()
    assert "3 Dateien geschrieben" in dialog.status_label.text()
    assert not dialog.btn_write.isEnabled()


def test_dialog_detect_selects_matching_release(app, dialog, monkeypatch):
    monkeypatch.setattr(sf, "detect_firmware", lambda port: FirmwareVersion("3.3.2", "2025.12.28"))
    dialog.btn_detect.click()
    settle(app, dialog)
    settle(app, dialog)
    assert dialog.release_combo.currentText() == "V3.3.2_2025.12.28"
    assert "passende Release" in dialog.detect_label.text()


def test_dialog_detect_without_version_keeps_choice(app, dialog, monkeypatch):
    monkeypatch.setattr(sf, "detect_firmware", lambda port: None)
    dialog.btn_detect.click()
    settle(app, dialog)
    settle(app, dialog)
    assert dialog.release_combo.currentText() == TAG
    assert "keine Version" in dialog.detect_label.text()


def test_dialog_failure_is_shown_and_retry_works(app, dialog, github, monkeypatch):
    def broken(tag, language_dir):
        raise OSError("kein Netz")

    original = sf.fetch_manifest
    monkeypatch.setattr(sf, "fetch_manifest", broken)
    dialog.btn_retry.click()
    settle(app, dialog)
    assert "kein Netz" in dialog.status_label.text()
    assert not dialog.btn_write.isEnabled()

    monkeypatch.setattr(sf, "fetch_manifest", original)
    dialog.btn_retry.click()
    settle(app, dialog)
    assert "Fehlt: 3" in dialog.summary_label.text()


def test_dialog_asks_before_overwriting(app, dialog, tmp_path, monkeypatch):
    from gui import system_files_dialog as module
    (tmp_path / "mp3").mkdir()
    (tmp_path / "mp3" / "0001.mp3").write_bytes(b"eigene Aufnahme")
    dialog.btn_retry.click()
    settle(app, dialog)
    monkeypatch.setattr(module, "confirm_action", lambda *a, **k: False)
    dialog.btn_write.click()
    settle(app, dialog)
    assert (tmp_path / "mp3" / "0001.mp3").read_bytes() == b"eigene Aufnahme"

    dialog.replace_check.setChecked(False)  # nur fehlende Dateien: keine Rueckfrage, Aufnahme bleibt
    dialog.btn_write.click()
    settle(app, dialog)
    settle(app, dialog)
    assert (tmp_path / "mp3" / "0001.mp3").read_bytes() == b"eigene Aufnahme"
    assert (tmp_path / "advert" / "0001.mp3").exists()
