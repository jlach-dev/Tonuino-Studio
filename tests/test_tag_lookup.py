import json
import shutil
import subprocess
from types import SimpleNamespace

import pytest

from core import album_identity, cover_sources, musicbrainz, tag_lookup
from core.metadata import MetadataManager, TrackMetadata
from core.musicbrainz import ReleaseTrack
from core.tag_lookup import (
    FIELD_ALBUM, FIELD_ARTIST, FIELD_MB_IDS, FIELD_TITLE, FIELD_TRACK, FIELD_YEAR, LocalTrack, ReleaseCandidate,
)

RELEASE_ID = "11111111-2222-3333-4444-555555555555"
GROUP_ID = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"


def local(index, title="", duration=0.0, track_number=0, album="MausHörspiel lang", artist="Otfried Preußler"):
    return LocalTrack(f"E:/01/{index:03d}.mp3", f"{index:03d}.mp3", index,
                      TrackMetadata(title=title, artist=artist, album=album, track_number=track_number,
                                    duration=duration))


def release_track(position, title, length):
    return ReleaseTrack(position, title, "Otfried Preußler", length, f"rec{position}", f"trk{position}")


def fake_mb_get(responses):
    def _get(url, accept="application/json", attempts=3):
        for prefix, body in responses.items():
            if url.startswith(prefix):
                return json.dumps(body).encode()
        raise AssertionError(f"unerwartete URL {url}")
    return _get


def test_musicbrainz_release_search_and_lookup_numbers_media_consecutively(monkeypatch):
    monkeypatch.setattr(musicbrainz, "_http_get", fake_mb_get({
        "https://musicbrainz.org/ws/2/release/?": {"releases": [
            {"id": RELEASE_ID, "title": "Die kleine Hexe", "score": 100, "date": "2004-09-13", "track-count": 3,
             "artist-credit": [{"name": "Otfried Preußler", "artist": {"id": "art1"}}],
             "release-group": {"id": GROUP_ID}},
            {"id": "low", "title": "Irgendwas", "score": 30},
        ]},
        f"https://musicbrainz.org/ws/2/release/{RELEASE_ID}": {
            "id": RELEASE_ID, "title": "Die kleine Hexe", "date": "2004-09-13",
            "artist-credit": [{"name": "Otfried Preußler", "artist": {"id": "art1"}}],
            "release-group": {"id": GROUP_ID},
            "media": [
                {"track-count": 2, "tracks": [
                    {"id": "t1", "position": 1, "title": "Heia Walpurgisnacht", "length": 1500000,
                     "recording": {"id": "r1"}},
                    {"id": "t2", "position": 2, "title": "Rabe Abraxas", "recording": {"id": "r2", "length": 1200000}},
                ]},
                {"track-count": 1, "tracks": [
                    {"id": "t3", "position": 1, "title": "Der größte Streich", "length": 1800000,
                     "recording": {"id": "r3"}},
                ]},
            ],
        },
    }))
    hits = musicbrainz.search_releases("Die kleine Hexe", "Otfried Preußler")
    assert [h.id for h in hits] == [RELEASE_ID]
    assert hits[0].track_count == 3 and hits[0].release_group_id == GROUP_ID and hits[0].artist_ids == ["art1"]

    release = musicbrainz.lookup_release(RELEASE_ID)
    assert [(t.position, t.title, t.length) for t in release.tracks] == [
        (1, "Heia Walpurgisnacht", 1500.0), (2, "Rabe Abraxas", 1200.0), (3, "Der größte Streich", 1800.0)]
    assert release.tracks[0].track_id == "t1" and release.tracks[0].recording_id == "r1"
    assert release.tracks[1].artist == "Otfried Preußler"  # vom Release geerbt


def test_search_candidates_prefers_matching_track_count(monkeypatch):
    monkeypatch.setattr(musicbrainz, "_http_get", fake_mb_get({
        "https://musicbrainz.org/ws/2/release/?": {"releases": [
            {"id": "mb-cd", "title": "Die kleine Hexe (CD)", "score": 100, "track-count": 12},
            {"id": "mb-2", "title": "Die kleine Hexe", "score": 95, "track-count": 2},
        ]},
    }))

    def _get(url, accept="*/*", max_bytes=0, headers=None, data=None):
        if url.startswith("https://api.deezer.com/search/album"):
            return json.dumps({"data": [{"id": 77, "title": "Die kleine Hexe", "nb_tracks": 2,
                                         "artist": {"name": "Otfried Preußler"}, "link": "https://dz/77"}]}).encode()
        if url.startswith("https://itunes.apple.com/search"):
            return json.dumps({"results": [{"collectionId": 9, "collectionName": "Die kleine Hexe", "trackCount": 5,
                                            "artistName": "Otfried Preußler", "releaseDate": "2010-01-01T00:00:00Z"}]}).encode()
        raise AssertionError(url)

    monkeypatch.setattr(cover_sources, "_get", _get)
    monkeypatch.setattr(cover_sources, "spotify_credentials", lambda: None)  # Spotify ohne Zugangsdaten: uebersprungen
    status = []
    found = tag_lookup.search_candidates("Die kleine Hexe", "Otfried Preußler", track_count=2, on_status=status.append)
    assert [(c.source, c.id) for c in found] == [
        ("MusicBrainz", "mb-2"), ("Deezer", "77"), ("MusicBrainz", "mb-cd"), ("iTunes", "9")]
    assert found[3].year == "2010"
    assert status == ["Suche bei MusicBrainz …", "Suche bei Deezer …", "Suche bei iTunes …"]

    # eine Quelle faellt aus - die anderen liefern trotzdem
    monkeypatch.setattr(musicbrainz, "_http_get", lambda *a, **k: (_ for _ in ()).throw(OSError("offline")))
    found = tag_lookup.search_candidates("Die kleine Hexe", sources=["MusicBrainz", "Deezer"])
    assert [c.source for c in found] == ["Deezer"]


def test_load_tracks_deezer_and_itunes(monkeypatch):
    def _get(url, accept="*/*", max_bytes=0, headers=None, data=None):
        if url == "https://api.deezer.com/album/77":
            return json.dumps({"release_date": "2004-09-13"}).encode()
        if url == "https://api.deezer.com/album/77/tracks":
            return json.dumps({"data": [{"title": "Teil 1", "duration": 1500, "artist": {"name": "Sprecher"}},
                                        {"title": "Teil 2", "duration": 1800}]}).encode()
        if url.startswith("https://itunes.apple.com/lookup"):
            return json.dumps({"results": [
                {"wrapperType": "collection", "collectionId": 9},
                {"wrapperType": "track", "trackName": "B", "trackNumber": 1, "discNumber": 2, "trackTimeMillis": 1000},
                {"wrapperType": "track", "trackName": "A", "trackNumber": 1, "discNumber": 1, "trackTimeMillis": 2000},
            ]}).encode()
        raise AssertionError(url)

    monkeypatch.setattr(cover_sources, "_get", _get)
    deezer = tag_lookup.load_tracks(ReleaseCandidate("Deezer", "77", "Die kleine Hexe", "Otfried Preußler"))
    assert deezer.year == "2004" and deezer.track_count == 2
    assert [(t.position, t.title, t.artist, t.length) for t in deezer.tracks] == [
        (1, "Teil 1", "Sprecher", 1500.0), (2, "Teil 2", "Otfried Preußler", 1800.0)]

    itunes = tag_lookup.load_tracks(ReleaseCandidate("iTunes", "9", "X"))
    assert [(t.position, t.title, t.length) for t in itunes.tracks] == [(1, "A", 2.0), (2, "B", 1.0)]
    assert itunes.tracks_loaded and tag_lookup.load_tracks(itunes) is itunes


def test_match_tracks_position_duration_then_duration_then_title():
    release = [release_track(1, "Heia Walpurgisnacht", 1500), release_track(2, "Rabe Abraxas", 1200),
               release_track(3, "Der größte Streich", 1800), release_track(4, "Bonus", 300)]
    tracks = [
        local(1, "Die kleine Hexe (1/2): Heia Walpurgisnacht | MausHörspiel lang", 1501.0),  # Position + Laenge
        local(2, "irgendein Titel", 1799.0),  # nur Laenge (Position 2 passt nicht)
        local(3, "Die kleine Hexe (2/2): Rabe Abraxas | MausHörspiel lang", 0.0),  # Titel
        local(4, "Unbekannt", 999.0),  # gleiche Anzahl -> nur Position
    ]
    matches = tag_lookup.match_tracks(tracks, release)
    assert [(m[0].position if m[0] else None, m[1]) for m in matches] == [
        (1, tag_lookup.MATCH_POSITION_DURATION), (3, tag_lookup.MATCH_DURATION),
        (2, tag_lookup.MATCH_TITLE), (4, tag_lookup.MATCH_POSITION)]

    # verschiedene Anzahl: ohne Laenge/Titel bleibt der Track ohne Zuordnung
    matches = tag_lookup.match_tracks([local(1, "x", 50.0), local(2, "y", 60.0)], release)
    assert matches == [(None, ""), (None, "")]


def test_build_diff_marks_changes_and_musicbrainz_ids(monkeypatch):
    monkeypatch.setattr(album_identity, "read_mb_ids", lambda path: {"release_id": "", "release_group_id": ""})
    candidate = ReleaseCandidate("MusicBrainz", RELEASE_ID, "Die kleine Hexe", "Otfried Preußler", "2004-09-13", 2,
                                 release_group_id=GROUP_ID, artist_ids=["art1"],
                                 tracks=[release_track(1, "Heia Walpurgisnacht", 1500),
                                         release_track(2, "Der größte Streich", 1800)], tracks_loaded=True)
    # drei lokale Tracks zu zwei Release-Tracks: kein Rueckfall auf die Position
    diffs = tag_lookup.build_diff([local(1, "Alt 1", 1500.0, track_number=1), local(2, "Zusatz", 42.0),
                                   local(3, "Extra", 7.0)], candidate)
    first, second, _third = diffs
    assert first.matched and first.new[FIELD_ALBUM] == "Die kleine Hexe" and first.new[FIELD_TRACK] == "1/2"
    assert first.new[FIELD_YEAR] == "2004" and first.changed(FIELD_TITLE) and first.changed(FIELD_ALBUM)
    assert not first.changed(FIELD_ARTIST)  # Interpret ist schon richtig
    assert first.mb_ids[album_identity.MB_ALBUM_ID_DESC] == RELEASE_ID
    assert first.mb_ids[album_identity.MB_RELEASE_GROUP_ID_DESC] == GROUP_ID
    assert first.mb_ids[tag_lookup.MB_RELEASE_TRACK_ID_DESC] == "trk1"
    assert first.changed(FIELD_MB_IDS)
    assert not second.matched and second.new == {}

    deezer = ReleaseCandidate("Deezer", "77", "Die kleine Hexe", tracks=candidate.tracks, tracks_loaded=True)
    assert tag_lookup.build_diff([local(1, "Alt 1", 1500.0)], deezer)[0].mb_ids == {}


@pytest.fixture
def mp3_pair(tmp_path):
    import imageio_ffmpeg
    first = tmp_path / "001.mp3"
    subprocess.run([imageio_ffmpeg.get_ffmpeg_exe(), "-loglevel", "error", "-f", "lavfi", "-i",
                    "anullsrc=r=22050:cl=mono", "-t", "0.3", "-q:a", "9", str(first)], check=True)
    second = tmp_path / "002.mp3"
    shutil.copy(first, second)
    return [first, second]


def test_apply_diff_writes_only_selected_fields(mp3_pair):
    manager = MetadataManager()
    for i, path in enumerate(mp3_pair, start=1):
        manager.write_metadata(str(path), title=f"Alt {i}", album="MausHörspiel lang", artist="Otfried Preußler")
    folder = SimpleNamespace(tracks=[SimpleNamespace(filepath=str(p), filename=p.name, index=i)
                                     for i, p in enumerate(mp3_pair, start=1)])
    local_tracks = tag_lookup.read_local_tracks(folder, manager)
    assert [t.meta.title for t in local_tracks] == ["Alt 1", "Alt 2"]
    assert tag_lookup.suggest_search(local_tracks) == ("MausHörspiel lang", "Otfried Preußler")

    candidate = ReleaseCandidate("MusicBrainz", RELEASE_ID, "Die kleine Hexe", "Otfried Preußler", "2004", 2,
                                 release_group_id=GROUP_ID,
                                 tracks=[release_track(1, "Heia Walpurgisnacht", 0.3),
                                         release_track(2, "Der größte Streich", 0.3)], tracks_loaded=True)
    diffs = tag_lookup.build_diff(local_tracks, candidate)
    assert [d.method for d in diffs] == [tag_lookup.MATCH_POSITION_DURATION] * 2

    written = tag_lookup.apply_diff(diffs, {0: [FIELD_ALBUM, FIELD_TITLE, FIELD_TRACK, FIELD_MB_IDS],
                                            1: [FIELD_ALBUM]}, manager)
    assert written == 2
    meta1 = manager.read_metadata(str(mp3_pair[0]))
    assert (meta1.album, meta1.title, meta1.track_number, meta1.total_tracks) == ("Die kleine Hexe", "Heia Walpurgisnacht", 1, 2)
    assert meta1.year == ""  # Jahr war nicht angehakt
    assert album_identity.read_mb_ids(str(mp3_pair[0])) == {"release_id": RELEASE_ID, "release_group_id": GROUP_ID}
    assert album_identity.read_txxx(str(mp3_pair[0]), tag_lookup.MB_RELEASE_TRACK_ID_DESC) == "trk1"
    meta2 = manager.read_metadata(str(mp3_pair[1]))
    assert (meta2.album, meta2.title) == ("Die kleine Hexe", "Alt 2")
    assert album_identity.read_mb_ids(str(mp3_pair[1]))["release_id"] == ""


def test_spotify_candidates_and_tracks(monkeypatch):
    calls = []

    def _get(url, accept="*/*", max_bytes=0, headers=None, data=None):
        calls.append(url)
        if url == "https://accounts.spotify.com/api/token":
            return json.dumps({"access_token": "tok", "expires_in": 3600}).encode()
        assert headers == {"Authorization": "Bearer tok"}
        if url.startswith("https://api.spotify.com/v1/search?"):
            return json.dumps({"albums": {"items": [
                {"id": "sp1", "name": "Die kleine Hexe", "total_tracks": 2, "release_date": "2004-09-13",
                 "artists": [{"name": "Otfried Preußler"}], "external_urls": {"spotify": "https://open.spotify.com/x"}}]}}).encode()
        if url.startswith("https://api.spotify.com/v1/albums/sp1/tracks"):
            if "offset" not in url:
                return json.dumps({"items": [
                    {"name": "Teil 2", "disc_number": 1, "track_number": 2, "duration_ms": 1800000},
                ], "next": "https://api.spotify.com/v1/albums/sp1/tracks?offset=50"}).encode()
            return json.dumps({"items": [
                {"name": "Teil 1", "disc_number": 1, "track_number": 1, "duration_ms": 1500000,
                 "artists": [{"name": "Sprecher"}]}], "next": None}).encode()
        raise AssertionError(url)

    monkeypatch.setattr(cover_sources, "_get", _get)
    monkeypatch.setattr(cover_sources, "spotify_credentials", lambda: ("id", "secret"))
    cover_sources._spotify_token.update(value="", expires=0.0, client_id="")
    assert tag_lookup.spotify_available()
    found = tag_lookup.search_candidates("Die kleine Hexe", "Otfried Preußler", sources=["Spotify"])
    assert [(c.source, c.id, c.track_count, c.year, c.url) for c in found] == [
        ("Spotify", "sp1", 2, "2004", "https://open.spotify.com/x")]
    loaded = tag_lookup.load_tracks(found[0])
    assert [(t.position, t.title, t.artist, t.length) for t in loaded.tracks] == [
        (1, "Teil 1", "Sprecher", 1500.0), (2, "Teil 2", "Otfried Preußler", 1800.0)]
    assert calls.count("https://accounts.spotify.com/api/token") == 1  # Token wird wiederverwendet

    monkeypatch.setattr(cover_sources, "spotify_credentials", lambda: None)
    assert not tag_lookup.spotify_available()
    assert tag_lookup.search_candidates("Die kleine Hexe", sources=["Spotify"]) == []
