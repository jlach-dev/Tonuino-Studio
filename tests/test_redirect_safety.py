"""Zugangsdaten duerfen keiner Umleitung und keiner Adresse aus einer Antwort folgen"""
import http.server
import threading
import urllib.error
from types import SimpleNamespace

import httpx
import pytest

from core import cover_research, cover_sources, tag_lookup

FAKE_KEY = "FAKE-KEY-NUR-FUER-DEN-TEST"


@pytest.fixture
def redirect_server():
    """Lokaler Server: /start leitet auf /ziel um; merkt sich die Anfragen samt Authorization"""
    seen = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            seen.append((self.path, self.headers.get("Authorization")))
            if self.path == "/start":
                self.send_response(302)
                self.send_header("Location", "/ziel")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            self.send_response(200)
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"ok")

        def log_message(self, *args):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}", seen
    server.shutdown()
    server.server_close()
    thread.join()


def test_get_with_authorization_stops_at_redirect(redirect_server):
    base, seen = redirect_server
    with pytest.raises(urllib.error.HTTPError) as error:
        cover_sources._get(f"{base}/start", headers={"Authorization": f"Bearer {FAKE_KEY}"})
    assert error.value.code == 302
    assert [path for path, _ in seen] == ["/start"]


def test_get_without_authorization_still_follows_redirect(redirect_server):
    base, seen = redirect_server
    assert cover_sources._get(f"{base}/start") == b"ok"  # Cover-Downloads brauchen das
    assert [path for path, _ in seen] == ["/start", "/ziel"]


def test_spotify_next_to_foreign_host_is_refused(monkeypatch):
    calls = []

    def fake_get(url, **kwargs):
        calls.append(url)
        if url.endswith("/api/token"):
            return b'{"access_token": "tok", "expires_in": 3600}'
        return b'{"items": [], "next": "https://fremd.example/seite2"}'

    monkeypatch.setattr(cover_sources, "_get", fake_get)
    monkeypatch.setattr(cover_sources, "spotify_credentials", lambda: ("id", "secret"))
    cover_sources._spotify_token.update(value="", expires=0.0, client_id="")

    with pytest.raises(ValueError):
        tag_lookup._load_spotify_tracks(SimpleNamespace(id="sp1", artist=""))
    assert not [url for url in calls if "fremd.example" in url]


def test_gemini_key_does_not_follow_redirect(monkeypatch):
    seen = []

    def handle(self, request):
        seen.append((request.url.host, request.headers.get("x-goog-api-key")))
        if request.url.host == "fremd.example":
            return httpx.Response(200, json={}, request=request)
        return httpx.Response(302, headers={"location": "https://fremd.example/x"}, request=request)

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", handle)
    client = cover_research.make_client(FAKE_KEY)
    with pytest.raises(Exception):
        client.interactions.create(model="gemini-test", input="hallo")

    assert seen, "Der Test hat keine Anfrage abgefangen"
    assert all(host != "fremd.example" for host, _ in seen), seen
    assert seen[0][1] == FAKE_KEY  # der eigene Endpunkt bekommt den Key weiterhin
