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
