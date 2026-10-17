import io
import json

from PIL import Image

from core import cover_sources
from core.cover_research import CoverBrief, FolderInfo, clean_track_title, suggest_search_title


def png_bytes(size=(400, 400)):
    buffer = io.BytesIO()
    Image.new("RGB", size, (10, 20, 30)).save(buffer, "PNG")
    return buffer.getvalue()


def fake_get(responses):
    def _get(url, accept="*/*", max_bytes=0, headers=None, data=None):
        for prefix, body in responses.items():
            if url.startswith(prefix):
                return body if isinstance(body, bytes) else json.dumps(body).encode()
        raise AssertionError(f"unerwartete URL {url}")
    return _get


def test_deezer_and_itunes_parsing(monkeypatch):
    monkeypatch.setattr(cover_sources, "_get", fake_get({
        "https://api.deezer.com/": {"data": [{"title": "Folge 39", "artist": {"name": "Die Fuchsbande"},
                                              "cover_big": "https://dz/500", "cover_xl": "https://dz/1000"}]},
        "https://itunes.apple.com/": {"results": [{"collectionName": "Folge 39", "artistName": "Die Fuchsbande",
                                                   "artworkUrl100": "https://is1/x/100x100bb.jpg"}]},
    }))
    deezer = cover_sources.search_deezer("Folge 39", "Die Fuchsbande")
    assert deezer[0].full_url == "https://dz/1000"
    itunes = cover_sources.search_itunes("Folge 39")
    assert itunes[0].preview_url.endswith("600x600bb.jpg")
    assert itunes[0].full_url.endswith("1200x1200bb.jpg")


def test_spotify_uses_client_credentials(monkeypatch):
    seen = []

    def _get(url, accept="*/*", max_bytes=0, headers=None, data=None):
        seen.append((url, headers))
        if url.startswith("https://accounts.spotify.com/"):
            assert headers["Authorization"].startswith("Basic ")
            assert data == b"grant_type=client_credentials"
            return json.dumps({"access_token": "tok", "expires_in": 3600}).encode()
        assert headers["Authorization"] == "Bearer tok"
        return json.dumps({"albums": {"items": [{"name": "Folge 39", "artists": [{"name": "Die Fuchsbande"}],
                                                 "images": [{"url": "https://sp/300", "width": 300},
                                                            {"url": "https://sp/640", "width": 640}]}]}}).encode()

    monkeypatch.setattr(cover_sources, "_get", _get)
    monkeypatch.setattr(cover_sources, "spotify_credentials", lambda: ("id", "secret"))
    cover_sources._spotify_token.update(value="", expires=0.0, client_id="")

    covers = cover_sources.search_spotify("Folge 39")
    cover_sources.search_spotify("Folge 40")  # Token wird wiederverwendet
    assert covers[0].full_url == "https://sp/640"
    assert sum(1 for url, _ in seen if "accounts.spotify.com" in url) == 1


def test_page_images_reads_og_image(monkeypatch):
    html = b'<html><head><meta property="og:image" content="/img/cover-39.jpg?x=1&amp;y=2"></head></html>'
    monkeypatch.setattr(cover_sources, "_get", fake_get({"https://verlag.de/": html}))
    assert cover_sources.page_images("https://verlag.de/folge-39") == ["https://verlag.de/img/cover-39.jpg?x=1&y=2"]


def test_search_all_dedupes_and_skips_small_images(monkeypatch):
    found = []
    monkeypatch.setattr(cover_sources, "SEARCHERS", {
        "A": lambda t, a="": [cover_sources.OnlineCover("A", "t", "", "u1", "u1"),
                              cover_sources.OnlineCover("A", "t", "", "small", "small")],
        "B": lambda t, a="": [cover_sources.OnlineCover("B", "t", "", "u2", "u2")],
    })
    images = {"u1": png_bytes(), "u2": png_bytes(), "small": None}
    monkeypatch.setattr(cover_sources, "download_image", lambda url, min_edge=250: images[url])

    count = cover_sources.search_all("t", "", ["A", "B"], on_found=lambda c, d: found.append(c.source))
    assert count == 1 and found == ["A"]


def test_download_image_rejects_tiny_and_non_images(monkeypatch):
    monkeypatch.setattr(cover_sources, "_get", lambda url, accept="*/*": {"big": png_bytes(), "tiny": png_bytes((50, 50)),
                                                                          "html": b"<html>"}[url])
    assert cover_sources.download_image("big")
    assert cover_sources.download_image("tiny") is None
    assert cover_sources.download_image("html") is None


def test_search_title_never_uses_folder_name():
    info = FolderInfo(album="", artist="Die Fuchsbande",
                      titles=["01 - Die kleine Hexe (1/2): Heia Walpurgisnacht | MausHörspiel lang (07.12.2024)"])
    assert suggest_search_title(info) == "Die kleine Hexe"
    # erkannte Reihe aus den Tracktiteln schlaegt Album-Tag (Feed) und Recherche-Titel
    assert suggest_search_title(info, CoverBrief(album_title="Folge 39: Fall 77")) == "Die kleine Hexe"
    assert suggest_search_title(FolderInfo(titles=["Intro"]), CoverBrief(album_title="Folge 39")) == "Folge 39"
    assert suggest_search_title(FolderInfo(album="Album-Tag"), CoverBrief(album_title="x")) == "Album-Tag"
    assert clean_track_title("Bibi Blocksberg - Teil 2") == "Bibi Blocksberg"


def test_musicbrainz_search_falls_back_to_simplified_title(monkeypatch):
    from core import musicbrainz

    queries = []

    def fake_get(url, accept="application/json", attempts=3):
        from urllib.parse import parse_qs, urlparse
        query = parse_qs(urlparse(url).query)["query"][0]
        queries.append(query)
        # nur der vereinfachte Titel ohne Interpret trifft
        if 'releasegroup:"Eule findet den Beat"' in query and "artist:" not in query:
            return json.dumps({"release-groups": [{"id": "rg", "title": "Eule findet den Beat", "score": 100,
                                                   "artist-credit": [{"name": "Eule"}]}]}).encode()
        return json.dumps({"release-groups": []}).encode()

    monkeypatch.setattr(musicbrainz, "_http_get", fake_get)
    hits = musicbrainz.search_release_groups("Eule findet den Beat (Musik-Hörspiel)", "Eule")
    assert [h.title for h in hits] == ["Eule findet den Beat"]
    assert queries[0].startswith('releasegroup:"Eule findet den Beat \\(Musik')
    assert len(queries) == 4  # exakt+Interpret, exakt, vereinfacht+Interpret, vereinfacht

    queries.clear()
    assert musicbrainz.search_release_groups("Gibt es nicht", "X") == []
    assert any(q.startswith("releasegroup:(Gibt AND es AND nicht)") for q in queries)
