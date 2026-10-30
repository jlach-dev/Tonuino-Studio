"""Dateien nach Tags einsortieren (C3)"""

import os

import pytest

from core.library_organize import DEFAULT_SCHEME, execute_moves, plan_moves, safe_name
from core.metadata import MetadataManager
from tests.test_library import make_album


def test_safe_name():
    assert safe_name('Die kleine Hexe: "Teil 1"?') == "Die kleine Hexe_ _Teil 1__"
    assert safe_name("  Ende.  ") == "Ende"
    assert safe_name("") == "Unbekannt"
    assert safe_name("...And You Will Know Us") == "_And You Will Know Us"  # kein versteckter Ordner
    assert safe_name("Con") == "Con_" and safe_name("nul.txt") == "nul.txt_" and safe_name("Console") == "Console"
    assert not safe_name("x" * 119 + ". y").endswith((".", " "))  # erst kuerzen, dann Enden saeubern


def test_organize_moves_cover_along_and_keeps_user_dotfiles(tmp_path):
    root = tmp_path / "Musik"
    folder = make_album(root, "Unsortiert", count=2, album="Die kleine Hexe", artist="Preußler")
    (folder / "cover.jpg").write_bytes(b"cover")
    (folder / "Thumbs.db").write_bytes(b"x")
    plans = plan_moves([str(folder / "01.mp3"), str(folder / "02.mp3")], str(root))
    result = execute_moves(plans)
    target = root / "Preußler" / "Die kleine Hexe"
    assert result.moved == 2 and (target / "cover.jpg").read_bytes() == b"cover"
    assert not folder.exists() and result.removed_dirs == 1  # nur Thumbs.db blieb - Ordner weg

    other = make_album(root, "Zweites", count=1, album="Anderes", artist="Jemand")
    (other / ".meine-notizen").write_text("bleibt")
    execute_moves(plan_moves([str(other / "01.mp3")], str(root)))
    assert (other / ".meine-notizen").read_text() == "bleibt"  # Ordner bleibt stehen


def test_too_long_target_is_reported_before_moving(tmp_path):
    root = tmp_path / "Musik"
    folder = make_album(root, "U", count=1, album="A" * 120, artist="B" * 120)
    plans = plan_moves([str(folder / "01.mp3")], str(root), scheme="{albumartist}/{album}/{album}/{title}")
    assert plans[0].status == "too_long" and not plans[0].will_move


def test_plan_and_execute_moves(tmp_path):
    root = tmp_path / "Musik"
    folder = make_album(root, "Unsortiert", count=3, album="Die kleine Hexe", artist="Preußler")
    MetadataManager().write_metadata(str(folder / "03.mp3"), album="", title="")  # ohne Tags
    files = [str(folder / f"{i:02d}.mp3") for i in (1, 2, 3)]
    existing = root / "Preußler" / "Die kleine Hexe" / "02 - Track 2.mp3"
    existing.parent.mkdir(parents=True)
    existing.write_bytes(b"schon da")

    plans = plan_moves(files, str(root))
    assert [p.status for p in plans] == ["ok", "conflict", "missing_tags"]
    assert plans[0].target == str(root / "Preußler" / "Die kleine Hexe" / "01 - Track 1.mp3")

    result = execute_moves(plans)
    assert result.moved == 1 and result.errors == []
    assert os.path.exists(plans[0].target) and not os.path.exists(files[0])
    assert existing.read_bytes() == b"schon da" and os.path.exists(files[1]) and os.path.exists(files[2])
    assert result.removed_dirs == 0  # Ursprungsordner hat noch Dateien

    # alles verschoben -> leerer Ursprungsordner wird entfernt; bereits richtige Dateien bleiben
    MetadataManager().write_metadata(files[2], album="Die kleine Hexe", title="Track 3", track_number=3)
    existing.unlink()
    plans = plan_moves(files[1:], str(root))
    assert [p.status for p in plans] == ["ok", "ok"]
    result = execute_moves(plans)
    assert result.moved == 2 and result.removed_dirs == 1 and not folder.exists()
    again = plan_moves([plans[0].target], str(root))
    assert again[0].status == "unchanged"


def test_custom_scheme(tmp_path):
    root = tmp_path / "Musik"
    folder = make_album(root, "A", count=1, album="Album", artist="Wer")
    plan = plan_moves([str(folder / "01.mp3")], str(root), "{artist} - {album}/{title}")[0]
    assert plan.target == str(root / "Wer - Album" / "Track 1.mp3")
    broken = plan_moves([str(folder / "01.mp3")], str(root), "{nicht_da}")[0]
    assert broken.target.endswith(DEFAULT_SCHEME.split("/")[-1].format(track=1, title="Track 1") + ".mp3")


def test_organize_dialog_preview(tmp_path):
    pytest.importorskip("PyQt6")
    from PyQt6.QtCore import QCoreApplication, QSettings
    from PyQt6.QtWidgets import QApplication
    from gui import organize_dialog
    from gui.organize_dialog import OrganizeDialog

    QCoreApplication.setOrganizationName("Tonuino-Manager-Tests")
    app = QApplication.instance() or QApplication([])
    assert app is not None
    root = tmp_path / "Musik"
    folder = make_album(root, "Unsortiert", count=2, album="Die kleine Hexe", artist="Preußler")
    files = [str(folder / "01.mp3"), str(folder / "02.mp3")]
    dialog = OrganizeDialog(files, str(root), None, label="Unsortiert")
    assert dialog.table.rowCount() == 2 and dialog.btn_apply.isEnabled()
    assert "2 Datei(en) werden verschoben" in dialog.summary.text()
    organize_dialog.confirm_action = lambda *a, **k: True
    dialog._apply()
    assert dialog.result.moved == 2
    assert (root / "Preußler" / "Die kleine Hexe" / "01 - Track 1.mp3").exists()
    QSettings().clear()


def _set_disc(path, value):
    from mutagen.id3 import ID3, TPOS
    tags = ID3(path)
    tags.add(TPOS(encoding=3, text=value))
    tags.save(path)


def test_multi_disc_albums_get_disc_prefix(tmp_path):
    from core.library import read_track_tags
    root = tmp_path / "Musik"
    cd1 = make_album(root, "CD1", count=2, album="Die kleine Hexe", artist="Preußler")
    cd2 = make_album(root, "CD2", count=1, album="Die kleine Hexe", artist="Preußler")
    for path in (cd1 / "01.mp3", cd1 / "02.mp3"):
        _set_disc(str(path), "1/2")
    _set_disc(str(cd2 / "01.mp3"), "2/2")
    assert (read_track_tags(str(cd2 / "01.mp3")).disc_number, read_track_tags(str(cd2 / "01.mp3")).disc_total) == (2, 2)

    plans = plan_moves([str(cd1 / "01.mp3"), str(cd1 / "02.mp3"), str(cd2 / "01.mp3")], str(root))
    names = [os.path.basename(p.target) for p in plans]
    assert names == ["1-01 - Track 1.mp3", "1-02 - Track 2.mp3", "2-01 - Track 1.mp3"]
    assert all(p.will_move for p in plans)  # gleiche Nummer und gleicher Titel kollidieren nicht mehr

    single = make_album(root, "Einzeln", count=1, album="Anderes Album", artist="Jemand")
    _set_disc(str(single / "01.mp3"), "1/1")
    assert os.path.basename(plan_moves([str(single / "01.mp3")], str(root))[0].target) == "01 - Track 1.mp3"
