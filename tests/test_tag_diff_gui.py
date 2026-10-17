import shutil
import subprocess

import pytest

pytest.importorskip("PyQt6")

from PyQt6.QtCore import QCoreApplication, Qt
from PyQt6.QtWidgets import QApplication, QDialog

from core import album_identity, tag_lookup
from core.metadata import MetadataManager
from core.musicbrainz import ReleaseTrack
from core.sd_card import SDCard
from core.tag_lookup import FIELD_ALBUM, FIELD_TITLE, ReleaseCandidate
from gui import tag_diff_dialog
from gui.tag_diff_dialog import COL_FIELD0, COL_TRACK, TagDiffDialog

RELEASE_ID = "11111111-2222-3333-4444-555555555555"


@pytest.fixture(scope="module")
def app():
    QCoreApplication.setOrganizationName("Tonuino-Manager-Tests")
    QCoreApplication.setApplicationName("tests")
    return QApplication.instance() or QApplication([])


@pytest.fixture
def sd(tmp_path):
    import imageio_ffmpeg
    folder = tmp_path / "sd" / "01"
    folder.mkdir(parents=True)
    subprocess.run([imageio_ffmpeg.get_ffmpeg_exe(), "-loglevel", "error", "-f", "lavfi", "-i",
                    "anullsrc=r=22050:cl=mono", "-t", "0.3", "-q:a", "9", str(folder / "001.mp3")], check=True)
    shutil.copy(folder / "001.mp3", folder / "002.mp3")
    manager = MetadataManager()
    for i in (1, 2):
        manager.write_metadata(str(folder / f"00{i}.mp3"), album="MausHörspiel lang", artist="Otfried Preußler",
                               title=f"Die kleine Hexe ({i}/2): Teil {i} | MausHörspiel lang (07.12.2024)")
    card = SDCard(str(tmp_path / "sd"))
    card.scan()
    return card


def column(key):
    return COL_FIELD0 + [k for k, _l in tag_lookup.FIELDS].index(key)


def run_workers(app, dialog):
    for _ in range(4):
        for worker in (dialog._search_worker, dialog._tracks_worker):
            if worker is not None:
                worker.wait(5000)
        app.processEvents()


@pytest.fixture
def fake_lookup(monkeypatch):
    calls = []

    def search(title, artist="", sources=(), track_count=0, on_status=None, cancelled=lambda: False):
        calls.append((title, artist, list(sources), track_count))
        return [ReleaseCandidate("MusicBrainz", RELEASE_ID, "Die kleine Hexe", "Otfried Preußler", "2004-09-13", 2,
                                 "https://musicbrainz.org/release/" + RELEASE_ID, 100, "rg-1")]

    def load(candidate):
        candidate.tracks = [ReleaseTrack(1, "Heia Walpurgisnacht", "Otfried Preußler", 0.3, "r1", "t1"),
                            ReleaseTrack(2, "Der größte Streich", "Otfried Preußler", 0.3, "r2", "t2")]
        candidate.tracks_loaded = True
        return candidate

    monkeypatch.setattr(tag_lookup, "search_candidates", search)
    monkeypatch.setattr(tag_lookup, "load_tracks", load)
    monkeypatch.setattr(tag_diff_dialog, "confirm_action", lambda *a, **k: True)
    return calls


def test_tag_diff_dialog_searches_from_tags_and_applies_checked_cells(app, sd, fake_lookup):
    folder = sd.folders[1]
    dialog = TagDiffDialog(folder, MetadataManager())
    run_workers(app, dialog)

    # Suchbegriff aus den Tracktiteln (Reihe), nicht der Feed-Name
    assert fake_lookup[0][0] == "Die kleine Hexe" and fake_lookup[0][1] == "Otfried Preußler"
    assert fake_lookup[0][3] == 2
    assert dialog.title_edit.text() == "Die kleine Hexe"
    assert dialog.release_combo.count() == 1 and dialog.candidate is not None
    assert dialog.table.rowCount() == 2
    assert all(d.method == tag_lookup.MATCH_POSITION_DURATION for d in dialog.diffs)

    album_cell = dialog.table.item(0, column(FIELD_ALBUM))
    assert album_cell.text() == "MausHörspiel lang → Die kleine Hexe"
    assert album_cell.checkState() == Qt.CheckState.Checked
    assert dialog.btn_apply.isEnabled()

    # Titel-Spalte komplett abwaehlen, Zeile 2 ganz abwaehlen
    dialog.field_checks[FIELD_TITLE].setChecked(False)
    dialog.table.item(1, COL_TRACK).setCheckState(Qt.CheckState.Unchecked)
    selection = dialog.selection()
    assert 1 not in selection and FIELD_TITLE not in selection[0]

    dialog._apply()
    assert dialog.result() == QDialog.DialogCode.Accepted
    assert dialog.written == 1 and dialog.applied_album == "Die kleine Hexe"

    manager = MetadataManager()
    meta1 = manager.read_metadata(folder.tracks[0].filepath)
    assert meta1.album == "Die kleine Hexe" and meta1.title.startswith("Die kleine Hexe (1/2)")
    assert (meta1.track_number, meta1.total_tracks, meta1.year) == (1, 2, "2004")
    assert album_identity.read_mb_ids(folder.tracks[0].filepath) == {"release_id": RELEASE_ID, "release_group_id": "rg-1"}
    meta2 = manager.read_metadata(folder.tracks[1].filepath)
    assert meta2.album == "MausHörspiel lang"


def test_tag_diff_dialog_without_candidates(app, sd, monkeypatch):
    monkeypatch.setattr(tag_lookup, "search_candidates", lambda *a, **k: [])
    dialog = TagDiffDialog(sd.folders[1], MetadataManager(), title="Gibt es nicht")
    run_workers(app, dialog)
    assert dialog.release_combo.count() == 0
    assert not dialog.btn_apply.isEnabled()
    assert "Kein Release" in dialog.status_label.text()
    dialog.done(0)


def test_cover_manager_has_tag_button(app, sd, tmp_path, monkeypatch):
    from gui import cover_manager_dialog as cm
    from core.cover_archive import CoverArchive

    monkeypatch.setattr(cm, "gemini_client", lambda: None)
    dialog = cm.CoverManagerDialog(sd.folders[1], "Ordner 01", MetadataManager(), CoverArchive(str(tmp_path / "data")))
    assert dialog.btn_tags.text() == "Tags von MusicBrainz …"
    assert dialog.tags_changed is False
    for worker in (dialog._research_worker, dialog._search_worker):
        if worker is not None:
            worker.wait(5000)
    app.processEvents()
    dialog.done(0)
