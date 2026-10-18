"""Cover-Stapelsuche: Bewertung, Vorschlaege, Uebernehmen, Pruefdialog"""

import pytest

from core import cover_batch
from core.cover_archive import KIND_FILE, ROLE_ALBUM_COVER, CoverArchive
from core.cover_batch import AlbumProposal, CoverHit, is_certain, propose, propose_all, rank_hits, score_hit
from core.library import LibraryAlbum, LibraryIndex, read_track_tags
from core.metadata import MetadataManager
from tests.test_library import make_album, png_bytes

HEXE = LibraryAlbum(path="x", title="Die kleine Hexe", artist="Otfried Preußler", tracks=["a", "b", "c"])


def test_score_and_certainty():
    exact = CoverHit("Deezer", "Die kleine Hexe", "Otfried Preußler", "u1", track_count=3)
    other_episode = CoverHit("Deezer", "Die kleine Hexe – Folge 2", "Otfried Preußler", "u2", track_count=5)
    unrelated = CoverHit("iTunes", "Räuber Hotzenplotz", "Otfried Preußler", "u3", track_count=3)
    assert score_hit(HEXE, exact) == 100.0 and "3 Tracks ✓" in exact.reasons
    assert score_hit(HEXE, other_episode) < score_hit(HEXE, exact)
    assert "5 statt 3 Tracks" in other_episode.reasons
    assert score_hit(HEXE, unrelated) < 60
    ranked = rank_hits(HEXE, [unrelated, other_episode, exact])
    assert ranked[0] is exact and is_certain(ranked)
    # zwei gleich gute Treffer: unsicher
    twin = CoverHit("iTunes", "Die kleine Hexe", "Otfried Preußler", "u4", track_count=3)
    assert not is_certain(rank_hits(HEXE, [exact, twin]))
    assert not is_certain([])
    # ohne Interpret und Trackanzahl: neutral, aber nicht sicher genug fuer sich allein
    bare = LibraryAlbum(path="y", title="Die kleine Hexe")
    assert score_hit(bare, CoverHit("Deezer", "Die kleine Hexe", "", "u")) == 65.0


def test_propose_prefers_archive_then_release_id_then_search(tmp_path, monkeypatch):
    archive = CoverArchive(str(tmp_path / "data"))
    downloads = []

    def download(url):
        downloads.append(url)
        return None if "fail" in url else png_bytes(size=(300, 300))

    # 1. Archiv
    archive.ensure_album("id1", "Die kleine Hexe")
    entry = archive.add_cover("id1", png_bytes(size=(320, 320)), KIND_FILE, source="t")
    archive.set_role("id1", entry.id, ROLE_ALBUM_COVER)
    linked = LibraryAlbum(path="x", title="Die kleine Hexe", tracks=["a"], album_id="id1")
    proposal = propose(linked, download=download, archive=archive)
    assert proposal.certain and proposal.hit.source == cover_batch.SOURCE_ARCHIVE and downloads == []

    # 2. Release-ID
    tagged = LibraryAlbum(path="x", title="Die kleine Hexe", tracks=["a"], mb_release_id="rel-1")
    proposal = propose(tagged, download=download, archive=archive)
    assert proposal.certain and proposal.hit.source == cover_batch.SOURCE_RELEASE_ID
    assert downloads == ["https://coverartarchive.org/release/rel-1/front-1200"]

    # 3. Suche: bester Treffer nicht ladbar -> naechster, dann unsicher (Spotify: kein Netz im Test)
    monkeypatch.setattr(cover_batch.tag_lookup, "spotify_available", lambda: False)
    monkeypatch.setitem(cover_batch.SEARCHERS, "Deezer", lambda t, a: [
        CoverHit("Deezer", "Die kleine Hexe", "Otfried Preußler", "fail-best", track_count=3),
        CoverHit("Deezer", "Die kleine Hexe (Hörspiel)", "Otfried Preußler", "second", track_count=3)])
    monkeypatch.setitem(cover_batch.SEARCHERS, "iTunes", lambda t, a: (_ for _ in ()).throw(OSError("offline")))
    proposal = propose(HEXE, sources=["Deezer", "iTunes", "Spotify"], download=download, archive=archive)
    assert proposal.usable and proposal.hit.image_url == "second" and not proposal.certain
    assert proposal.note.startswith("bitte prüfen") and len(proposal.alternatives) == 1

    assert propose(LibraryAlbum(path="x", tracks=["a"]), download=download).note.startswith("kein Album-Tag")
    monkeypatch.setitem(cover_batch.SEARCHERS, "Deezer", lambda t, a: [])
    assert propose(HEXE, sources=["Deezer"], download=download).note == "nichts gefunden"


def test_propose_all_and_apply(tmp_path, monkeypatch):
    archive = CoverArchive(str(tmp_path / "data"))
    folder = make_album(tmp_path / "Musik", "Hexe", count=2, album="Die kleine Hexe", artist="Preußler")
    album = LibraryAlbum(path=str(folder), title="Die kleine Hexe", artist="Preußler",
                         tracks=[str(folder / "01.mp3"), str(folder / "02.mp3")])
    covered = LibraryAlbum(path="y", title="Hat schon", tracks=["a"], has_cover=True)
    offline = LibraryAlbum(path="z", title="Weg", tracks=["a"], offline=True)
    monkeypatch.setitem(cover_batch.SEARCHERS, "Deezer", lambda t, a: [
        CoverHit("Deezer", "Die kleine Hexe", "Preußler", "https://dz/xl", "https://dz/page", track_count=2)])
    seen = []
    proposals = propose_all(LibraryIndex(albums=[album, covered, offline]), archive, ["Deezer"],
                            progress=lambda i, n, name: seen.append(name), download=lambda url: png_bytes())
    assert seen == ["Die kleine Hexe"] and len(proposals) == 1 and proposals[0].certain

    result = cover_batch.apply_proposals(proposals, archive)
    assert (result.albums, result.tracks, result.skipped) == (1, 2, 0)
    assert (folder / "cover.jpg").exists()
    assert MetadataManager().get_cover_bytes(str(folder / "02.mp3"))[:3] == b"\xff\xd8\xff"
    # Album wurde im Archiv angelegt, Dateien mit der ID getaggt, Cover mit Rolle Album-Cover
    album_id = read_track_tags(str(folder / "01.mp3")).album_id
    assert album_id and album.album_id == album_id
    covers = archive.covers(album_id)
    assert len(covers) == 1 and ROLE_ALBUM_COVER in covers[0].roles and covers[0].provider == "Deezer"
    assert archive.load_album(album_id).album_cover_sha1


def test_cover_proposal_dialog(tmp_path, monkeypatch):
    pytest.importorskip("PyQt6")
    from PyQt6.QtCore import QCoreApplication, QSettings
    from PyQt6.QtWidgets import QApplication
    from gui import cover_proposal_dialog
    from gui.cover_proposal_dialog import CoverProposalDialog

    QCoreApplication.setOrganizationName("Tonuino-Manager-Tests")
    app = QApplication.instance() or QApplication([])
    assert app is not None
    archive = CoverArchive(str(tmp_path / "data"))
    folder = make_album(tmp_path / "Musik", "Hexe", count=1, album="Die kleine Hexe")
    album = LibraryAlbum(path=str(folder), title="Die kleine Hexe", tracks=[str(folder / "01.mp3")])
    sure = AlbumProposal(album, CoverHit("Deezer", "Die kleine Hexe", "", "u1", score=90), png_bytes(), True, "sicher")
    alt = CoverHit("iTunes", "Die kleine Hexe (Lesung)", "", "u2", score=60, reasons=["Titel 80 %"])
    unsure = AlbumProposal(LibraryAlbum(path="y", title="Räuber", tracks=["a"]),
                           CoverHit("Deezer", "Räuber?", "", "u3", score=55), png_bytes(), False, "bitte prüfen",
                           alternatives=[alt])
    empty = AlbumProposal(LibraryAlbum(path="z", title="Nix", tracks=["a"]), note="nichts gefunden")
    dialog = CoverProposalDialog([sure, unsure, empty], archive)
    assert [c.isChecked() for c in dialog.checks] == [True, False, False]
    assert not dialog.checks[2].isEnabled()
    assert "1 sicher, 1 bitte prüfen, 1 ohne Treffer" in dialog.summary.text()

    dialog._use_alternative(1, alt, download=lambda url: png_bytes(color=(1, 2, 3)))
    assert unsure.hit is alt and dialog.checks[1].isChecked() and unsure.alternatives[0].image_url == "u3"
    dialog.checks[1].setChecked(False)
    assert [p.album.title for p in dialog.selected_proposals()] == ["Die kleine Hexe"]

    cover_proposal_dialog.confirm_action = lambda *a, **k: True
    dialog._apply()
    assert dialog.result.albums == 1 and (folder / "cover.jpg").exists()

    dialog2 = CoverProposalDialog([sure], archive)
    dialog2.table.selectRow(0)
    dialog2._open_in_manager()
    assert dialog2.open_requested is album
    QSettings().clear()
