import os
import stat
import sys
from pathlib import Path

import pytest

from core.sd_card import SDCard


def make_card(root: Path, layout: dict) -> SDCard:
    """layout: {"01": ["001.mp3", ...], ...} - Dateiinhalt ist der Dateiname"""
    for folder, files in layout.items():
        (root / folder).mkdir()
        for name in files:
            (root / folder / name).write_bytes(name.encode())
    card = SDCard(str(root))
    card.scan()
    return card


def names(path: Path):
    return sorted(p.name for p in path.iterdir())


def test_scan_detects_folders_and_tracks(tmp_path):
    card = make_card(tmp_path, {"01": ["001.mp3", "002.mp3"], "mp3": ["0001.mp3"], "misc": []})

    assert card.is_valid_tonuino
    assert list(card.folders) == [1]
    assert card.folders[1].track_count == 2
    assert "mp3" in card.special_folders


def test_scan_ignores_appledouble_files(tmp_path):
    card = make_card(tmp_path, {"01": ["001.mp3", "._001.mp3"]})
    assert [t.filename for t in card.folders[1].tracks] == ["001.mp3"]


def test_reorder_tracks_swaps_files(tmp_path):
    card = make_card(tmp_path, {"01": ["001.mp3", "002.mp3", "003.mp3"]})
    folder = card.folders[1]
    first, second, third = folder.tracks

    card.reorder_tracks(folder, [third, first, second])

    assert names(tmp_path / "01") == ["001.mp3", "002.mp3", "003.mp3"]
    assert (tmp_path / "01" / "001.mp3").read_bytes() == b"003.mp3"
    assert (tmp_path / "01" / "002.mp3").read_bytes() == b"001.mp3"
    assert [t.index for t in folder.tracks] == [1, 2, 3]


def test_delete_tracks_closes_gaps(tmp_path):
    card = make_card(tmp_path, {"01": ["001.mp3", "002.mp3", "003.mp3"]})
    folder = card.folders[1]

    card.delete_tracks(folder, [folder.tracks[1]])

    assert names(tmp_path / "01") == ["001.mp3", "002.mp3"]
    assert (tmp_path / "01" / "002.mp3").read_bytes() == b"003.mp3"


def test_reorder_failure_restores_original_names(tmp_path, monkeypatch):
    card = make_card(tmp_path, {"01": ["001.mp3", "002.mp3", "003.mp3"]})
    folder = card.folders[1]
    tracks = list(folder.tracks)

    real_rename = Path.rename
    failed = []

    def failing_rename(self, target):
        # Zweiter Schritt (temporaer -> final) scheitert einmalig an der zweiten Datei
        if not failed and self.name.startswith(".tmp_") and Path(target).name == "002.mp3":
            failed.append(True)
            raise PermissionError("gesperrt")
        return real_rename(self, target)

    monkeypatch.setattr(Path, "rename", failing_rename)

    with pytest.raises(PermissionError):
        card.reorder_tracks(folder, [tracks[2], tracks[0], tracks[1]])

    monkeypatch.undo()
    assert names(tmp_path / "01") == ["001.mp3", "002.mp3", "003.mp3"]
    assert (tmp_path / "01" / "001.mp3").read_bytes() == b"001.mp3"
    assert (tmp_path / "01" / "003.mp3").read_bytes() == b"003.mp3"


def test_purge_removes_foreign_items_and_empty_folders(tmp_path):
    card = make_card(tmp_path, {"02": ["001.mp3", "notes.txt", "._001.mp3"], "05": [], "advert": ["0001.mp3"]})
    (tmp_path / "readme.txt").write_text("x")
    (tmp_path / "System Volume Information").mkdir()

    preview = card.preview_purge()
    assert preview.root_files == ["readme.txt"]
    assert preview.root_dirs == ["System Volume Information"]
    assert preview.foreign_items_in_folders == 2

    card.purge()

    assert names(tmp_path) == ["02", "advert"]
    assert names(tmp_path / "02") == ["001.mp3"]


def test_purge_keeps_trackless_folders_with_relevant_files(tmp_path):
    card = make_card(tmp_path, {
        "01": ["001.mp3"],
        "02": ["01 - Titel.mp3", "notes.txt"],  # MP3s mit anderem Namen
        "03": ["card.png", "card.json"],  # nur Kartencover
        "04": [".tmp_001.mp3"],  # Rest eines abgebrochenen Umnummerierens
        "05": ["notes.txt", "._001.mp3"],  # nur Fremddateien
        "06": [],
    })
    (tmp_path / "07").mkdir()
    (tmp_path / "07" / ".tmp_import").mkdir()  # Rest eines abgebrochenen Ersetzens
    (tmp_path / "07" / ".tmp_import" / "001.mp3").write_bytes(b"neu")

    preview = card.preview_purge()
    assert [(f.name, f.file_count, f.has_card) for f in preview.folders_to_delete] == [
        ("05", 2, False), ("06", 0, False)]
    assert [(f.name, f.has_card) for f in preview.folders_kept_without_tracks] == [
        ("02", False), ("03", True), ("04", False), ("07", False)]

    card.purge()

    assert names(tmp_path) == ["01", "02", "03", "04", "07"]
    assert names(tmp_path / "02") == ["01 - Titel.mp3"]
    assert names(tmp_path / "03") == ["card.json", "card.png"]
    assert names(tmp_path / "04") == [".tmp_001.mp3"]
    assert (tmp_path / "07" / ".tmp_import" / "001.mp3").read_bytes() == b"neu"


@pytest.mark.skipif(sys.platform != "win32", reason="Nur-Lesen-Attribut auf Ordnern gibt es nur unter Windows")
def test_purge_deletes_read_only_folders(tmp_path):
    # Google Drive/OneDrive (und macOS auf FAT-Karten) setzen das Nur-Lesen-
    # Attribut auf Ordnern - os.rmdir() scheitert daran mit WinError 5
    card = make_card(tmp_path, {"01": ["001.mp3"], "05": [], "06": ["notes.txt"]})
    for name in ("05", "06", "06/notes.txt"):
        os.chmod(tmp_path / name, stat.S_IREAD)

    card.purge()

    assert names(tmp_path) == ["01"]


def test_delete_folder_reports_failure_and_keeps_folder_entry(tmp_path, monkeypatch):
    card = make_card(tmp_path, {"01": ["001.mp3"]})

    def fail(path):
        raise PermissionError(5, "Zugriff verweigert", str(path))

    monkeypatch.setattr(os, "rmdir", fail)
    with pytest.raises(PermissionError):
        card.delete_folder(1)
    assert 1 in card.folders


def test_purge_confirmation_lists_deleted_and_kept_folders(tmp_path):
    pytest.importorskip("PyQt6")
    from core.sd_card import PurgeFolderInfo, PurgePreview
    from gui.main_window import MainWindow

    preview = PurgePreview(folders_to_delete=[PurgeFolderInfo("05", 2), PurgeFolderInfo("06")],
                           folders_kept_without_tracks=[PurgeFolderInfo("03", 2, has_card=True)])
    text = MainWindow._purge_confirmation_text(MainWindow, preview)
    assert "2 Ordner ohne Tracks werden gelöscht: 05 (2 Datei(en)), 06 (leer)" in text
    assert "03 (2 Datei(en), mit Kartencover)" in text
    assert "Keine Fremddateien" not in text


def test_purge_keeps_folder_numbers_but_closes_track_gaps(tmp_path):
    card = make_card(tmp_path, {"01": ["001.mp3"], "03": ["002.mp3", "005.mp3"], "07": ["001.mp3"]})

    card.purge()

    assert names(tmp_path) == ["01", "03", "07"]
    assert sorted(card.folders) == [1, 3, 7]
    assert names(tmp_path / "03") == ["001.mp3", "002.mp3"]
    assert (tmp_path / "03" / "002.mp3").read_bytes() == b"005.mp3"


def test_purge_handles_foreign_folder_named_like_a_track(tmp_path):
    card = make_card(tmp_path, {"01": ["002.mp3"]})
    (tmp_path / "01" / "001.mp3").mkdir()

    card.purge()

    assert names(tmp_path / "01") == ["001.mp3"]
    assert (tmp_path / "01" / "001.mp3").is_file()
    assert (tmp_path / "01" / "001.mp3").read_bytes() == b"002.mp3"


def test_close_track_gaps_renumbers_and_keeps_order(tmp_path):
    card = make_card(tmp_path, {"01": ["002.mp3", "005.mp3", "009.mp3"]})
    folder = card.folders[1]
    assert card.has_track_gaps(folder)

    assert card.close_track_gaps(folder) is True

    assert names(tmp_path / "01") == ["001.mp3", "002.mp3", "003.mp3"]
    assert [(tmp_path / "01" / n).read_bytes() for n in ("001.mp3", "002.mp3", "003.mp3")] == [b"002.mp3", b"005.mp3", b"009.mp3"]
    assert not card.has_track_gaps(folder)
    assert card.close_track_gaps(folder) is False


def test_close_track_gaps_does_not_rename_without_gaps(tmp_path):
    card = make_card(tmp_path, {"01": ["001.mp3", "002.mp3"]})
    folder = card.folders[1]
    mtimes = {p.name: p.stat().st_mtime_ns for p in (tmp_path / "01").iterdir()}

    assert card.close_track_gaps(folder) is False

    assert {p.name: p.stat().st_mtime_ns for p in (tmp_path / "01").iterdir()} == mtimes
    assert [t.index for t in folder.tracks] == [1, 2]


def test_delete_last_track_needs_no_renaming(tmp_path):
    card = make_card(tmp_path, {"01": ["001.mp3", "002.mp3", "003.mp3"]})
    folder = card.folders[1]

    card.delete_tracks(folder, [folder.tracks[2]])

    assert names(tmp_path / "01") == ["001.mp3", "002.mp3"]
    assert [t.filename for t in folder.tracks] == ["001.mp3", "002.mp3"]


def test_create_folder_validates_range(tmp_path):
    card = make_card(tmp_path, {"01": ["001.mp3"]})
    with pytest.raises(ValueError):
        card.create_folder(100)
    with pytest.raises(FileExistsError):
        card.create_folder(1)
