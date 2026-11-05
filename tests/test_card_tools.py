"""SD-Karte leeren, mehrere Ordner loeschen, Fortschrittsfenster, KI-Bildgroesse, fpcalc-Pruefsummen"""

import base64
import io
import os
import stat
from pathlib import Path
from types import SimpleNamespace

import pytest

pytest.importorskip("PyQt6")

from PIL import Image
from PyQt6.QtCore import QCoreApplication, QEvent, QSettings, Qt
from PyQt6.QtGui import QKeyEvent
from PyQt6.QtWidgets import QApplication, QProgressDialog

from core import acoustid, ai_settings, drive_check
from core.cover_generator import GenerationRequest, ImageRefused, SOURCE_AI, extend_to_card, generate_art
from core.cover_research import CoverBrief, FolderInfo
from core.sd_card import SDCard


@pytest.fixture(scope="module")
def app():
    QCoreApplication.setOrganizationName("Tonuino-Manager-Tests")
    QCoreApplication.setApplicationName("tests-card-tools")
    application = QApplication.instance() or QApplication([])
    QSettings().clear()
    yield application
    QSettings().clear()


def make_card(root: Path, folders=(1, 2, 3)) -> SDCard:
    for index in folders:
        path = root / f"{index:02d}"
        path.mkdir()
        (path / "001.mp3").write_bytes(b"x")
    card = SDCard(str(root))
    card.scan()
    return card


# --- Karte leeren ---------------------------------------------------------------------------------

def test_wipe_removes_everything_including_read_only(tmp_path):
    card = make_card(tmp_path, folders=(1,))
    (tmp_path / "mp3").mkdir()
    (tmp_path / "mp3" / "0001.mp3").write_bytes(b"abc")
    locked = tmp_path / "fremd.txt"
    locked.write_bytes(b"12")
    os.chmod(locked, stat.S_IREAD)
    (tmp_path / "System Volume Information").mkdir()

    preview = card.preview_wipe()
    assert (preview.file_count, preview.folder_count, preview.total_bytes) == (3, 2, 6)
    assert locked.exists()  # die Vorschau aendert nichts

    assert card.wipe() == []
    assert [p.name for p in tmp_path.iterdir()] == ["System Volume Information"]
    assert not card.folders


def test_wipe_reports_what_could_not_be_deleted(tmp_path, monkeypatch):
    card = make_card(tmp_path, folders=(1,))

    def fail(*args, **kwargs):
        raise OSError("gesperrt")

    monkeypatch.setattr("core.sd_card.shutil.rmtree", fail)
    errors = card.wipe()
    assert len(errors) == 1 and "gesperrt" in errors[0]


def test_wipe_refuses_anything_but_a_removable_drive_root(tmp_path, monkeypatch):
    monkeypatch.setattr(drive_check.os.path, "ismount", lambda p: False)
    monkeypatch.setattr(drive_check, "is_removable_drive", lambda p: True)
    assert "Hauptverzeichnis" in drive_check.wipe_refusal(tmp_path)

    monkeypatch.setattr(drive_check.os.path, "ismount", lambda p: True)
    monkeypatch.setattr(drive_check.Path, "home", lambda: tmp_path / "ich")
    assert "Benutzerordner" in drive_check.wipe_refusal(tmp_path)

    monkeypatch.setattr(drive_check.Path, "home", lambda: tmp_path.parent / "anderes" / "ich")
    monkeypatch.setattr(drive_check, "is_removable_drive", lambda p: False)
    assert "Wechseldatenträger" in drive_check.wipe_refusal(tmp_path)

    monkeypatch.setattr(drive_check, "is_removable_drive", lambda p: True)
    assert drive_check.wipe_refusal(tmp_path) is None
    monkeypatch.setattr(drive_check, "is_removable_drive", lambda p: None)  # macOS: nicht ermittelbar
    assert drive_check.wipe_refusal(tmp_path) is None


# --- Hauptfenster ----------------------------------------------------------------------------------

def open_window(app, card):
    from gui.main_window import MainWindow

    window = MainWindow()
    window.sd_card = card
    window._populate_folder_list()
    return window


def wait_for_card_job(app, window):
    while window._card_job is not None:
        app.processEvents()


def select_rows(window, *rows):
    window.folder_list.clearSelection()
    for row in rows:
        window.folder_list.item(row).setSelected(True)


def test_delete_several_selected_folders(app, tmp_path, monkeypatch):
    card = make_card(tmp_path)
    window = open_window(app, card)
    window._select_folder_in_list(1)
    select_rows(window, 0, 2)
    assert window.btn_delete_folder.text() == " Ordner löschen"  # der Knopf bleibt beim gezeigten Ordner

    questions = []
    monkeypatch.setattr("gui.main_window.confirm_action", lambda parent, title, text: questions.append(text) or True)
    window._sd_album_map = {"a": 1, "b": 2}
    window._delete_selected_folders()
    wait_for_card_job(app, window)

    assert sorted(card.folders) == [2]
    assert not (tmp_path / "01").exists() and (tmp_path / "02").exists() and not (tmp_path / "03").exists()
    assert "2 Ordner" in questions[0] and "RFID-Karten" in questions[0]
    assert window._sd_album_map == {"b": 2}
    assert window.current_folder is None
    assert "2 Ordner gelöscht" in window.status_bar.currentMessage()
    window.close()


def test_folder_menu_offers_folder_and_card_commands(app, tmp_path):
    window = open_window(app, make_card(tmp_path))
    window.show()
    app.processEvents()
    def labels(pos):
        return [a.text() for a in window._folder_menu(pos).actions() if a.text()]

    first = window.folder_list.visualItemRect(window.folder_list.item(0)).center()
    single = labels(first)
    assert window.current_folder.index == 1  # Rechtsklick waehlt den Ordner
    assert single[:3] == ["Umbenennen", "Ordnernummer ändern …", "Ordner löschen …"]
    assert "SD-Karte leeren und neu einrichten …" in single

    select_rows(window, 0, 1)
    several = labels(first)
    assert several[0] == "2 Ordner löschen …" and "Umbenennen" not in several

    below = window.folder_list.viewport().rect().bottomLeft()
    assert labels(below)[0] == "Neuer Ordner"  # freie Flaeche: nur Befehle fuer die Karte
    window.close()


def test_delete_key_asks_before_deleting_selection(app, tmp_path, monkeypatch):
    from PyQt6.QtGui import QShortcut

    card = make_card(tmp_path)
    window = open_window(app, card)
    window._select_folder_in_list(1)
    select_rows(window, 0, 1)
    questions = []
    monkeypatch.setattr("gui.main_window.confirm_action", lambda parent, title, text: questions.append(text) or False)
    shortcuts = {s.key().toString(): s for s in window.folder_list.findChildren(QShortcut)}
    shortcuts["Del"].activated.emit()
    assert "2 Ordner" in questions[0] and "wirklich" in questions[0]
    assert sorted(card.folders) == [1, 2, 3]
    window.close()


def test_change_folder_number_moves_or_swaps(app, tmp_path, monkeypatch):
    card = make_card(tmp_path, folders=(1, 2))
    (tmp_path / "01" / "marke.txt").write_bytes(b"x")
    window = open_window(app, card)
    window._select_folder_in_list(1)
    window._folder_name_cache = {1: "Eins", 2: "Zwei"}
    window._sd_album_map = {"a": 1}
    questions = []
    monkeypatch.setattr("gui.main_window.confirm_action", lambda parent, title, text: questions.append(text) or True)

    monkeypatch.setattr("gui.main_window.QInputDialog.getInt", lambda *a, **k: (2, True))
    window._change_folder_number()
    assert (tmp_path / "02" / "marke.txt").exists() and not (tmp_path / "01" / "marke.txt").exists()
    assert sorted(card.folders) == [1, 2] and window.current_folder is card.folders[2]
    assert window._folder_name_cache == {2: "Eins", 1: "Zwei"} and window._sd_album_map == {"a": 2}
    assert "tauschen" in questions[0] and "RFID" in questions[0]

    monkeypatch.setattr("gui.main_window.QInputDialog.getInt", lambda *a, **k: (7, True))
    window._change_folder_number()
    assert (tmp_path / "07" / "marke.txt").exists() and not (tmp_path / "02").exists()
    assert sorted(card.folders) == [1, 7]
    assert not [p for p in tmp_path.iterdir() if p.name.startswith(".tmp")]
    window.close()


def test_failed_swap_restores_the_occupied_folder(tmp_path, monkeypatch):
    card = make_card(tmp_path, folders=(1, 2))
    real_rename = os.rename

    def fail_second(src, dst):
        if Path(src).name == "01":
            raise OSError("gesperrt")
        real_rename(src, dst)

    monkeypatch.setattr("core.sd_card.os.rename", fail_second)
    with pytest.raises(OSError):
        card.change_folder_number(1, 2)
    assert sorted(p.name for p in tmp_path.iterdir()) == ["01", "02"]


def test_image_size_menu_is_easy_to_reach(app, tmp_path):
    QSettings().remove(ai_settings.SETTINGS_IMAGE_SIZE)
    window = open_window(app, make_card(tmp_path, folders=(1,)))
    window._sync_image_size_menu()
    actions = {a.data(): a for a in window.image_size_menu.actions()}
    assert actions["1K"].isChecked() and not actions["2K"].isChecked()
    actions["2K"].trigger()
    assert ai_settings.get_image_size() == "2K"
    QSettings().remove(ai_settings.SETTINGS_IMAGE_SIZE)
    window.close()


def test_delete_declined_keeps_folders(app, tmp_path, monkeypatch):
    card = make_card(tmp_path)
    window = open_window(app, card)
    window._select_folder_in_list(1)
    questions = []
    monkeypatch.setattr("gui.main_window.confirm_action", lambda parent, title, text: questions.append(text) or False)
    window._delete_folder()  # Knopf im Kopfbereich
    assert sorted(card.folders) == [1, 2, 3] and "RFID-Karten" in questions[0]
    window.close()


def test_special_folders_are_never_part_of_a_multi_delete(app, tmp_path):
    (tmp_path / "mp3").mkdir()
    (tmp_path / "mp3" / "0001.mp3").write_bytes(b"x")
    card = make_card(tmp_path, folders=(1, 2))
    window = open_window(app, card)
    window.folder_list.selectAll()
    assert [f.index for f in window._folders_to_delete()] == [1, 2]

    window.folder_list.clearSelection()
    window.current_folder = card.get_any_folder("mp3")
    assert window._folders_to_delete() == []
    window.close()


def test_ctrl_click_out_of_selection_does_not_switch_folder(app, tmp_path):
    window = open_window(app, make_card(tmp_path))
    window._select_folder_in_list(1)
    shown = window.current_folder
    select_rows(window, 0, 1)
    window.folder_list.item(1).setSelected(False)
    window._on_folder_selected(window.folder_list.item(1))
    assert window.current_folder is shown
    window.close()


def test_wipe_needs_typed_confirmation_then_offers_system_files(app, tmp_path, monkeypatch):
    card = make_card(tmp_path)
    window = open_window(app, card)
    monkeypatch.setattr("gui.main_window.wipe_refusal", lambda path: None)
    monkeypatch.setattr(window, "_confirm_purge_target_is_sd_card", lambda: True)
    monkeypatch.setattr("gui.main_window.confirm_action", lambda parent, title, text: True)
    opened = []
    monkeypatch.setattr(window, "_open_system_files", lambda: opened.append(1))

    monkeypatch.setattr("gui.main_window.QInputDialog.getText", lambda *a, **k: ("löschen", True))
    window._wipe_sd_card()
    assert sorted(card.folders) == [1, 2, 3] and window._card_job is None and not opened

    monkeypatch.setattr("gui.main_window.QInputDialog.getText", lambda *a, **k: ("LÖSCHEN", True))
    window._wipe_sd_card()
    wait_for_card_job(app, window)
    assert not card.folders and not list(tmp_path.iterdir()) and opened == [1]
    window.close()


def test_wipe_refusal_stops_before_any_question(app, tmp_path, monkeypatch):
    card = make_card(tmp_path)
    window = open_window(app, card)
    monkeypatch.setattr("gui.main_window.wipe_refusal", lambda path: "nein")
    warnings = []
    monkeypatch.setattr("gui.main_window.QMessageBox.warning", lambda parent, title, text: warnings.append(text))
    monkeypatch.setattr("gui.main_window.confirm_action", lambda *a: pytest.fail("darf nicht fragen"))
    window._wipe_sd_card()
    assert warnings == ["nein"] and sorted(card.folders) == [1, 2, 3]
    window.close()


def test_failed_replace_refreshes_list_and_current_folder(app, tmp_path, monkeypatch):
    card = make_card(tmp_path)
    window = open_window(app, card)
    window._select_folder_in_list(2)
    old = window.current_folder
    window._folder_name_cache[2] = "Veraltet"
    (tmp_path / "02" / "002.mp3").write_bytes(b"x")  # der halbe Austausch hat die Karte veraendert

    window._refresh_after_failed_import(rescan=True, folder_index=2)

    assert window.current_folder is not old and window.current_folder is card.folders[2]
    assert window.current_folder.track_count == 2
    assert 2 not in window._folder_name_cache
    assert window.folder_list.currentItem().data(Qt.ItemDataRole.UserRole) == 2
    window.close()


# --- Fortschrittsfenster ---------------------------------------------------------------------------

def test_x_and_escape_cancel_but_do_not_close_progress_dialog(app):
    from gui.common import close_progress_dialog, keep_open_until_done

    dialog = QProgressDialog("x", "Abbrechen", 0, 3)
    dialog.setMinimumDuration(0)
    cancelled = []
    keep_open_until_done(dialog, lambda: cancelled.append(1), "Gleich vorbei")
    dialog.show()

    dialog.close()
    assert dialog.isVisible() and cancelled == [1] and dialog.labelText() == "Gleich vorbei"
    QApplication.sendEvent(dialog, QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Escape, Qt.KeyboardModifier.NoModifier))
    assert dialog.isVisible() and cancelled == [1, 1]

    close_progress_dialog(dialog)
    assert not dialog.isVisible() and cancelled == [1, 1]  # das Ende ist kein Abbruch


def test_locked_dialog_without_cancel_stays_open(app):
    from gui.common import close_progress_dialog, lock_progress_dialog

    dialog = QProgressDialog("x", None, 0, 0)
    dialog.setMinimumDuration(0)
    lock_progress_dialog(dialog)
    dialog.show()
    dialog.close()
    assert dialog.isVisible()
    close_progress_dialog(dialog)
    assert not dialog.isVisible()


# --- KI-Bilder ------------------------------------------------------------------------------------

def image_client():
    buffer = io.BytesIO()
    Image.new("RGB", (4, 6)).save(buffer, "PNG")
    calls = []

    def create(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(output_image=SimpleNamespace(data=base64.b64encode(buffer.getvalue()).decode()),
                               output_text="")

    return SimpleNamespace(interactions=SimpleNamespace(create=create)), calls, buffer.getvalue()


def test_image_size_defaults_to_print_size_and_follows_setting(app):
    QSettings().remove(ai_settings.SETTINGS_IMAGE_SIZE)
    client, calls, png = image_client()
    req = GenerationRequest(info=FolderInfo(), brief=CoverBrief(), use_reference=False)

    generate_art(SOURCE_AI, req, client=client, model="m")
    assert calls[-1]["response_format"]["image_size"] == "1K"

    QSettings().setValue(ai_settings.SETTINGS_IMAGE_SIZE, "2K")
    extend_to_card(client, "m", png, "portrait")
    assert calls[-1]["response_format"]["image_size"] == "2K"

    QSettings().setValue(ai_settings.SETTINGS_IMAGE_SIZE, "unsinn")
    assert ai_settings.get_image_size() == "1K"
    QSettings().remove(ai_settings.SETTINGS_IMAGE_SIZE)


def test_settings_dialog_saves_image_size(app):
    from gui.card_cover_dialog import AiSettingsDialog

    QSettings().remove(ai_settings.SETTINGS_IMAGE_SIZE)
    dialog = AiSettingsDialog()
    assert dialog.image_size_combo.currentData() == "1K"
    dialog.image_size_combo.setCurrentIndex(dialog.image_size_combo.findData("2K"))
    dialog._save()
    assert ai_settings.get_image_size() == "2K"
    QSettings().remove(ai_settings.SETTINGS_IMAGE_SIZE)


def test_refused_image_explains_itself():
    client = SimpleNamespace(interactions=SimpleNamespace(
        create=lambda **k: SimpleNamespace(output_image=None, output_text="blocked")))
    with pytest.raises(RuntimeError) as caught:
        extend_to_card(client, "m", b"\x89PNG\r\n\x1a\n", "portrait")
    message = str(caught.value)
    assert "kein Fehler der App" in message and "Antwort von Google: blocked" in message
    assert "Ambient Blur" in message


def test_blocked_request_gives_tips_for_what_was_attempted():
    def blocked(**kwargs):
        raise RuntimeError("Error code: 400 - {'error': {'message': 'Request blocked for a reason.', "
                           "'code': 'content_blocked'}}")

    client = SimpleNamespace(interactions=SimpleNamespace(create=blocked))
    with pytest.raises(ImageRefused) as caught:
        extend_to_card(client, "m", b"\x89PNG\r\n\x1a\n", "portrait")
    assert "Häufige Gründe" in str(caught.value) and "Error code" not in str(caught.value)
    assert "Antwort von Google: Request blocked for a reason." in str(caught.value)
    with pytest.raises(ImageRefused) as caught:
        generate_art(SOURCE_AI, GenerationRequest(info=FolderInfo(), brief=CoverBrief(), use_reference=False),
                     client=client, model="m")
    assert "Als KI-Vorlage" in str(caught.value)


def test_extend_prompt_asks_for_the_reason_if_refused():
    client, calls, png = image_client()
    extend_to_card(client, "m", png, "portrait")
    assert "state the specific reason" in calls[-1]["input"][0]["text"]


def test_other_api_errors_stay_as_they_are():
    def broken(**kwargs):
        raise RuntimeError("Error code: 429 - quota")

    client = SimpleNamespace(interactions=SimpleNamespace(create=broken))
    with pytest.raises(RuntimeError, match="429") as caught:
        extend_to_card(client, "m", b"\x89PNG\r\n\x1a\n", "portrait")
    assert not isinstance(caught.value, ImageRefused)


# --- fpcalc ---------------------------------------------------------------------------------------

def test_fpcalc_checksums_cover_every_download():
    assert set(acoustid.FPCALC_BINARY_SHA256) == set(acoustid.FPCALC_DOWNLOADS)
    assert set(acoustid.FPCALC_SHA256) == set(acoustid.FPCALC_DOWNLOADS)
