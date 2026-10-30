"""Stuecke erkennen: fpcalc-Aufruf, AcoustID-Antwort, Gruppierung, Tag-Zuordnung (C2)"""

import hashlib
import io
import json
from types import SimpleNamespace

import pytest

from core import acoustid, tag_lookup
from core.library import read_track_tags
from core.metadata import MetadataManager
from core.musicbrainz import ReleaseTrack
from tests.test_library import make_album

LOOKUP = {
    "status": "ok",
    "results": [{
        "id": "x", "score": 0.93,
        "recordings": [{
            "id": "rec-1", "title": "Heia Walpurgisnacht", "artists": [{"name": "Otfried Preußler"}],
            "releasegroups": [{"id": "rg-hexe", "title": "Die kleine Hexe", "artists": [{"name": "Otfried Preußler"}]},
                              {"id": "rg-box", "title": "Hörspiel-Box", "artists": [{"name": "Various"}]}],
        }],
    }, {"id": "y", "score": 0.3, "recordings": [{"id": "rec-weak", "title": "Rauschen"}]}],
}


def test_parse_lookup_and_grouping():
    matches = acoustid.parse_lookup(LOOKUP)
    assert [m.release_group_id for m in matches] == ["rg-hexe", "rg-box"]  # schwacher Treffer (0.3) faellt weg
    assert matches[0].title == "Heia Walpurgisnacht" and matches[0].artist == "Otfried Preußler"

    files = [
        acoustid.FileResult("a.mp3", matches=matches),
        acoustid.FileResult("b.mp3", matches=[acoustid.Match(0.8, "rec-2", "Streich", "P", "rg-hexe", "Die kleine Hexe")]),
        acoustid.FileResult("c.mp3", matches=[acoustid.Match(0.9, "rec-3", "Fremd", "X", "rg-other", "Anderes")]),
        acoustid.FileResult("d.mp3", error="fpcalc fehlt"),
    ]
    result = acoustid.group_results(files)
    assert result.release_group_id == "rg-hexe" and result.release_group_title == "Die kleine Hexe"
    assert result.agreeing == 2 and result.recognized == 3
    assert result.recording_ids() == {"a.mp3": "rec-1", "b.mp3": "rec-2", "c.mp3": "rec-3"}


def test_fingerprint_and_recognize_with_fakes(tmp_path, monkeypatch):
    def fake_run(cmd, **kwargs):
        assert cmd[0] == "fpcalc" and cmd[1] == "-json"
        return SimpleNamespace(returncode=0, stdout=json.dumps({"duration": 123.4, "fingerprint": "AQAD"}), stderr="")

    monkeypatch.setattr(acoustid.subprocess, "run", fake_run)
    assert acoustid.fingerprint("x.mp3", "fpcalc") == (123.4, "AQAD")

    calls = []

    class FakeResponse:
        def __init__(self, data):
            self._data = json.dumps(data).encode()

        def read(self):
            return self._data

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def fake_urlopen(request, timeout=0):
        calls.append(request.data.decode())
        return FakeResponse(LOOKUP)

    monkeypatch.setattr(acoustid.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(acoustid, "_throttle", lambda: None)
    progress = []
    result = acoustid.recognize_files(["a.mp3", "b.mp3"], "KEY", "fpcalc",
                                      progress=lambda c, t, n: progress.append((c, t, n)))
    assert len(result.files) == 2 and result.release_group_id == "rg-hexe" and result.agreeing == 2
    assert "client=KEY" in calls[0] and "duration=123" in calls[0] and "fingerprint=AQAD" in calls[0]
    assert progress == [(1, 2, "a.mp3"), (2, 2, "b.mp3")]

    monkeypatch.setattr(acoustid.urllib.request, "urlopen",
                        lambda request, timeout=0: FakeResponse({"status": "error", "error": {"message": "invalid key"}}))
    bad = acoustid.recognize_files(["a.mp3"], "KEY", "fpcalc")
    assert "invalid key" in bad.files[0].error and bad.recognized == 0


def test_find_fpcalc_prefers_setting_then_data_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(acoustid.shutil, "which", lambda name: None)
    assert acoustid.find_fpcalc("", str(tmp_path)) is None
    tools = tmp_path / "tools"
    tools.mkdir()
    (tools / acoustid.fpcalc_name()).write_bytes(b"x")
    assert acoustid.find_fpcalc("", str(tmp_path)) == str(tools / acoustid.fpcalc_name())
    configured = tmp_path / "fpcalc-custom.exe"
    configured.write_bytes(b"x")
    assert acoustid.find_fpcalc(str(configured), str(tmp_path)) == str(configured)


def test_build_diff_uses_recording_ids(tmp_path):
    folder = make_album(tmp_path, "Hexe", count=2, album="Alt")
    local = tag_lookup.read_local_tracks([str(folder / "01.mp3"), str(folder / "02.mp3")], MetadataManager())
    candidate = tag_lookup.ReleaseCandidate(tag_lookup.SOURCE_MUSICBRAINZ, "rel", "Die kleine Hexe", "P")
    candidate.tracks = [ReleaseTrack(1, "Streich", "P", 0.3, recording_id="rec-2"),
                        ReleaseTrack(2, "Heia", "P", 0.3, recording_id="rec-1")]
    # ohne Recording-IDs: Zuordnung nach Position (01 -> Streich)
    assert tag_lookup.build_diff(local, candidate)[0].new[tag_lookup.FIELD_TITLE] == "Streich"
    # mit Recording-IDs aus der Erkennung: 01 ist die Aufnahme rec-1 -> Heia
    diffs = tag_lookup.build_diff(local, candidate, {str(folder / "01.mp3"): "rec-1", str(folder / "02.mp3"): "rec-2"})
    assert diffs[0].new[tag_lookup.FIELD_TITLE] == "Heia" and diffs[0].method == "acoustid"
    assert diffs[1].new[tag_lookup.FIELD_TITLE] == "Streich"


def test_candidates_from_release_group(monkeypatch):
    from core import musicbrainz

    def fake_get(url, accept="application/json", attempts=3):
        assert "release-group=rg-hexe" in url
        return json.dumps({"releases": [{"id": "rel-1", "title": "Die kleine Hexe", "date": "2007",
                                         "track-count": 2, "artist-credit": [{"name": "Otfried Preußler",
                                                                              "artist": {"id": "art-1"}}]}]}).encode()

    monkeypatch.setattr(musicbrainz, "_http_get", fake_get)
    candidates = tag_lookup.candidates_from_release_group("rg-hexe")
    assert len(candidates) == 1 and candidates[0].id == "rel-1" and candidates[0].release_group_id == "rg-hexe"
    assert candidates[0].track_count == 2 and candidates[0].source == tag_lookup.SOURCE_MUSICBRAINZ


def test_recognition_dialog_writes_best_matches(tmp_path):
    pytest.importorskip("PyQt6")
    from PyQt6.QtCore import QCoreApplication, QSettings
    from PyQt6.QtWidgets import QApplication
    from gui import recognition_dialog
    from gui.recognition_dialog import RecognitionDialog

    QCoreApplication.setOrganizationName("Tonuino-Manager-Tests")
    app = QApplication.instance() or QApplication([])  # Referenz halten, sonst stirbt die App im Test
    assert app is not None
    folder = make_album(tmp_path, "Lose", count=2)
    files = [acoustid.FileResult(str(folder / "01.mp3"), matches=[
        acoustid.Match(0.9, "rec-1", "Heia", "Preußler", "rg-1", "Die kleine Hexe", "Preußler")]),
        acoustid.FileResult(str(folder / "02.mp3"), error="nicht erkannt")]
    result = acoustid.group_results(files)
    dialog = RecognitionDialog(result, None, label="Lose")
    assert dialog.selected_rows() == [0]
    recognition_dialog.confirm_action = lambda *a, **k: True
    dialog._apply()
    assert dialog.written == 1
    tags = read_track_tags(str(folder / "01.mp3"))
    assert (tags.title, tags.artist, tags.album) == ("Heia", "Preußler", "Die kleine Hexe")
    assert MetadataManager().read_metadata(str(folder / "02.mp3")).title == "Track 2"
    QSettings().clear()


def test_tag_dialog_preset_skips_search(tmp_path, monkeypatch):
    pytest.importorskip("PyQt6")
    from PyQt6.QtCore import QCoreApplication, QSettings
    from PyQt6.QtWidgets import QApplication
    from gui.tag_diff_dialog import TagDiffDialog

    QCoreApplication.setOrganizationName("Tonuino-Manager-Tests")
    app = QApplication.instance() or QApplication([])
    monkeypatch.setattr(tag_lookup, "search_candidates", lambda *a, **k: pytest.fail("keine Suche bei Preset"))
    folder = make_album(tmp_path, "Hexe", count=2, album="Alt")
    files = [str(folder / "01.mp3"), str(folder / "02.mp3")]
    candidate = tag_lookup.ReleaseCandidate(tag_lookup.SOURCE_MUSICBRAINZ, "rel", "Die kleine Hexe", "P")
    candidate.tracks = [ReleaseTrack(1, "Heia", "P", 0.3, recording_id="rec-1"),
                        ReleaseTrack(2, "Streich", "P", 0.3, recording_id="rec-2")]
    candidate.tracks_loaded = True
    dialog = TagDiffDialog(files, MetadataManager(), None, "Alt", "", label="Hexe", preset=[candidate],
                           recording_ids={files[0]: "rec-2", files[1]: "rec-1"})
    for _ in range(20):
        app.processEvents()
    assert dialog.candidate is candidate
    assert dialog.diffs[0].new[tag_lookup.FIELD_TITLE] == "Streich" and dialog.diffs[0].method == "acoustid"
    assert "per Erkennung" in dialog.status_label.text()
    dialog._stop_workers()
    dialog.done(0)
    QSettings().clear()


def test_lookup_reports_acoustid_error(monkeypatch):
    import io
    import urllib.error
    from core import acoustid

    body = b'{"error": {"code": 4, "message": "invalid API key"}, "status": "error"}'

    def fail(request, timeout=0):
        raise urllib.error.HTTPError(acoustid.LOOKUP_URL, 400, "Bad Request", {}, io.BytesIO(body))

    monkeypatch.setattr(acoustid.urllib.request, "urlopen", fail)
    monkeypatch.setattr(acoustid, "_throttle", lambda: None)
    with pytest.raises(RuntimeError) as info:
        acoustid.lookup("falsch", 12.0, "AQAA")
    assert "Application-Key" in str(info.value) and "HTTP" not in str(info.value)


class _Download(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def _zip_with_fpcalc(content=b"MZ-fpcalc"):
    import zipfile
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("chromaprint-fpcalc-1.5.1-windows-x86_64/fpcalc.exe", content)
        archive.writestr("../../evil.exe", b"nein")  # wird nie ausgepackt
    return buffer.getvalue()


def test_download_fpcalc_checks_size_and_only_writes_fpcalc(tmp_path, monkeypatch):
    payload = _zip_with_fpcalc()
    monkeypatch.setattr(acoustid.urllib.request, "urlopen", lambda request, timeout=0: _Download(payload))
    monkeypatch.setattr(acoustid, "FPCALC_SIZES", {"win32": len(payload)})
    monkeypatch.setattr(acoustid, "FPCALC_SHA256", {"win32": hashlib.sha256(payload).hexdigest()})

    path = acoustid.download_fpcalc(str(tmp_path), platform="win32")

    assert open(path, "rb").read() == b"MZ-fpcalc"
    assert sorted(p.name for p in (tmp_path / "tools").iterdir()) == ["fpcalc.exe"]
    assert not (tmp_path.parent / "evil.exe").exists()


def test_download_fpcalc_rejects_wrong_size_checksum_and_unknown_system(tmp_path, monkeypatch):
    payload = _zip_with_fpcalc()
    monkeypatch.setattr(acoustid.urllib.request, "urlopen", lambda request, timeout=0: _Download(payload))

    monkeypatch.setattr(acoustid, "FPCALC_SIZES", {"win32": len(payload) + 1})
    with pytest.raises(RuntimeError, match="groß oder unvollständig"):
        acoustid.download_fpcalc(str(tmp_path), platform="win32")

    monkeypatch.setattr(acoustid, "FPCALC_SIZES", {"win32": len(payload)})
    monkeypatch.setattr(acoustid, "FPCALC_SHA256", {"win32": "0" * 64})
    with pytest.raises(RuntimeError, match="Prüfsumme"):
        acoustid.download_fpcalc(str(tmp_path), platform="win32")

    with pytest.raises(RuntimeError, match="kein fertiges fpcalc"):
        acoustid.download_fpcalc(str(tmp_path), platform="sunos5")
    assert not (tmp_path / "tools" / "fpcalc.exe").exists()


def test_pinned_fpcalc_checksums_cover_every_download():
    assert set(acoustid.FPCALC_SHA256) == set(acoustid.FPCALC_SIZES) == set(acoustid.FPCALC_DOWNLOADS)
    assert all(len(value) == 64 for value in acoustid.FPCALC_SHA256.values())
