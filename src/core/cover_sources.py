"""
Cover-Suche in mehreren Quellen (alle ausser Spotify ohne API-Key)

- Deezer      api.deezer.com/search/album     bis 1000 px, gute Abdeckung deutscher Hoerspiele
- iTunes      itunes.apple.com/search         bis 1200 px (URL-Groesse frei waehlbar)
- Spotify     api.spotify.com/v1/search       640 px, braucht Client-ID/Secret einer eigenen App
- MusicBrainz / Cover Art Archive             siehe core/musicbrainz.py
- Webseiten   og:image der Recherche-Quellen  (z.B. Verlagsseite mit Produktbild)

Die Bilder sind urheberrechtlich geschuetzt und nur fuer den privaten
Gebrauch (eigene Karten) gedacht.
"""

import base64
import hashlib
import html
import io
import json
import logging
import re
import threading
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Callable, Iterable, List, Optional

from PIL import Image

from core import ai_settings, musicbrainz
from core.musicbrainz import USER_AGENT

log = logging.getLogger(__name__)

SOURCE_DEEZER = "Deezer"
SOURCE_ITUNES = "iTunes"
SOURCE_SPOTIFY = "Spotify"
SOURCE_MUSICBRAINZ = "MusicBrainz"
SOURCE_WEB = "Webseite"
ALL_SOURCES = (SOURCE_DEEZER, SOURCE_SPOTIFY, SOURCE_ITUNES, SOURCE_MUSICBRAINZ, SOURCE_WEB)

HTTP_TIMEOUT = 15
MAX_IMAGE_BYTES = 8 * 1024 * 1024
MAX_HTML_BYTES = 2 * 1024 * 1024
MIN_IMAGE_EDGE = 250

# Hoefliche Mindestabstaende je Host (Deezer: 50 Anfragen/5 s, iTunes: ~20/min)
_HOST_DELAY = {"api.deezer.com": 0.2, "itunes.apple.com": 3.0}
_host_last: dict = {}
_host_lock = threading.Lock()


@dataclass
class OnlineCover:
    source: str
    title: str
    artist: str
    preview_url: str
    full_url: str
    page_url: str = ""
    back_preview_url: str = ""  # Rueckseite, falls die Quelle eine liefert (Cover Art Archive)
    back_full_url: str = ""

    @property
    def label(self) -> str:
        text = f"{self.source} · {self.title}" if self.title else self.source
        return f"{text} – {self.artist}" if self.artist else text


def _get(url: str, accept: str = "*/*", max_bytes: int = MAX_IMAGE_BYTES, headers: Optional[dict] = None,
         data: Optional[bytes] = None) -> bytes:
    host = urllib.parse.urlparse(url).hostname or ""
    delay = _HOST_DELAY.get(host, 0.0)
    if delay:
        with _host_lock:
            wait = delay - (time.monotonic() - _host_last.get(host, 0.0))
            if wait > 0:
                time.sleep(wait)
            _host_last[host] = time.monotonic()
    request = urllib.request.Request(url, data=data, headers={"User-Agent": USER_AGENT, "Accept": accept,
                                                               **(headers or {})})
    with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT) as response:
        body = response.read(max_bytes + 1)
    if len(body) > max_bytes:
        raise ValueError("Antwort zu gross")
    return body


def _get_json(url: str) -> dict:
    return json.loads(_get(url, accept="application/json"))


# --- Suchquellen ----------------------------------------------------------------------------

def search_deezer(title: str, artist: str = "", limit: int = 6) -> List[OnlineCover]:
    query = f"{artist} {title}".strip()
    data = _get_json("https://api.deezer.com/search/album?" + urllib.parse.urlencode({"q": query, "limit": limit}))
    result = []
    for album in data.get("data", [])[:limit]:
        full = album.get("cover_xl") or album.get("cover_big")
        if full:
            result.append(OnlineCover(SOURCE_DEEZER, album.get("title", ""), (album.get("artist") or {}).get("name", ""),
                                      album.get("cover_big") or full, full, album.get("link", "")))
    return result


def search_itunes(title: str, artist: str = "", limit: int = 6, country: str = "DE") -> List[OnlineCover]:
    query = f"{artist} {title}".strip()
    result = []
    for entity in ("album", "audiobook"):
        data = _get_json("https://itunes.apple.com/search?" + urllib.parse.urlencode(
            {"term": query, "entity": entity, "country": country, "limit": limit}))
        for item in data.get("results", []):
            art = item.get("artworkUrl100", "")
            if not art:
                continue
            # die Groesse steckt im Dateinamen (100x100bb) und ist frei waehlbar
            result.append(OnlineCover(SOURCE_ITUNES, item.get("collectionName", ""), item.get("artistName", ""),
                                      art.replace("100x100bb", "600x600bb"), art.replace("100x100bb", "1200x1200bb"),
                                      item.get("collectionViewUrl", "")))
        if result:
            break
    return result[:limit]


# Spotify: Token (Client-Credentials-Flow) gilt eine Stunde und wird wiederverwendet
_spotify_token = {"value": "", "expires": 0.0, "client_id": ""}
# (client_id, secret) aus Umgebung/Schluesselbund; in Tests austauschbar
spotify_credentials: Callable[[], Optional[tuple]] = ai_settings.get_spotify_credentials


def _spotify_access_token() -> str:
    credentials = spotify_credentials()
    if not credentials:
        raise RuntimeError("Keine Spotify-Zugangsdaten hinterlegt")
    client_id, secret = credentials
    if (_spotify_token["value"] and _spotify_token["client_id"] == client_id
            and time.monotonic() < _spotify_token["expires"] - 60):
        return _spotify_token["value"]
    auth = base64.b64encode(f"{client_id}:{secret}".encode()).decode()
    body = json.loads(_get("https://accounts.spotify.com/api/token", accept="application/json",
                           headers={"Authorization": f"Basic {auth}",
                                    "Content-Type": "application/x-www-form-urlencoded"},
                           data=b"grant_type=client_credentials"))
    _spotify_token.update(value=body["access_token"], client_id=client_id,
                          expires=time.monotonic() + float(body.get("expires_in", 3600)))
    return _spotify_token["value"]


def search_spotify(title: str, artist: str = "", limit: int = 6, market: str = "DE") -> List[OnlineCover]:
    token = _spotify_access_token()
    query = f"{artist} {title}".strip()
    data = json.loads(_get("https://api.spotify.com/v1/search?" + urllib.parse.urlencode(
        {"q": query, "type": "album", "market": market, "limit": limit}),
        accept="application/json", headers={"Authorization": f"Bearer {token}"}))
    result = []
    for album in (data.get("albums") or {}).get("items", []):
        images = sorted(album.get("images", []), key=lambda i: i.get("width") or 0, reverse=True)
        if not images:
            continue
        artists = ", ".join(a.get("name", "") for a in album.get("artists", []))
        result.append(OnlineCover(SOURCE_SPOTIFY, album.get("name", ""), artists, images[0]["url"],
                                  images[0]["url"], (album.get("external_urls") or {}).get("spotify", "")))
    return result


def search_musicbrainz(title: str, artist: str = "", limit: int = 6) -> List[OnlineCover]:
    result = []
    for group in musicbrainz.search_release_groups(title, artist)[:4]:
        images = musicbrainz.list_cover_art(group)
        fronts = [i for i in images if i.front] or images[:1]
        backs = [i for i in images if "Back" in i.types]
        for image in fronts[:1]:
            back = backs[0] if backs else None
            result.append(OnlineCover(SOURCE_MUSICBRAINZ, group.title, group.artist, image.url_500, image.url_1200,
                                      f"https://musicbrainz.org/release-group/{group.id}",
                                      back.url_500 if back else "", back.url_1200 if back else ""))
        if len(result) >= limit:
            break
    return result


_META_IMAGE = re.compile(
    r'<meta[^>]+(?:property|name)=["\'](?:og:image(?::secure_url)?|twitter:image)["\'][^>]*>', re.IGNORECASE)
_CONTENT = re.compile(r'content=["\']([^"\']+)["\']', re.IGNORECASE)


def page_images(page_url: str) -> List[str]:
    """Vorschaubilder einer Webseite (og:image / twitter:image)"""
    try:
        raw = _get(page_url, accept="text/html", max_bytes=MAX_HTML_BYTES)
    except Exception as e:
        log.info("Seite nicht ladbar %s: %s", page_url, e)
        return []
    text = raw.decode("utf-8", errors="ignore")
    urls = []
    for tag in _META_IMAGE.findall(text):
        match = _CONTENT.search(tag)
        if match:
            url = urllib.parse.urljoin(page_url, html.unescape(match.group(1)))
            if url.startswith("http") and url not in urls:
                urls.append(url)
    return urls


def web_covers(page_urls: Iterable[str], direct_urls: Iterable[str] = ()) -> List[OnlineCover]:
    """Kandidaten aus Recherche-Quellen: direkt gemeldete Bild-URLs und die
    Vorschaubilder der Quellseiten"""
    result = []
    for url in direct_urls:
        if url.startswith("http"):
            result.append(OnlineCover(SOURCE_WEB, "Recherche", urllib.parse.urlparse(url).hostname or "", url, url, url))
    for page in page_urls:
        for url in page_images(page)[:2]:
            # Weiterleitungs-Links der Google-Suche zeigen den eigentlichen Host erst nach dem Abruf
            result.append(OnlineCover(SOURCE_WEB, "Quelle", urllib.parse.urlparse(url).hostname or "", url, url, page))
    return result


# --- Laden & Pruefen ------------------------------------------------------------------------

def download_image(url: str, min_edge: int = MIN_IMAGE_EDGE) -> Optional[bytes]:
    """Laedt ein Bild und prueft, dass es wirklich eines ist und gross genug"""
    try:
        data = _get(url, accept="image/*")
        with Image.open(io.BytesIO(data)) as img:
            img.load()
            if min(img.size) < min_edge:
                return None
            if img.format not in ("JPEG", "PNG", "WEBP"):
                buffer = io.BytesIO()
                img.convert("RGB").save(buffer, "PNG")
                data = buffer.getvalue()
        return data
    except Exception as e:
        log.info("Bild nicht ladbar %s: %s", url, e)
        return None


SEARCHERS = {
    SOURCE_DEEZER: search_deezer,
    SOURCE_ITUNES: search_itunes,
    SOURCE_SPOTIFY: search_spotify,
    SOURCE_MUSICBRAINZ: search_musicbrainz,
}


def search_all(title: str, artist: str, sources: Iterable[str], page_urls: Iterable[str] = (),
               direct_urls: Iterable[str] = (), on_found: Optional[Callable[[OnlineCover, bytes], None]] = None,
               on_status: Optional[Callable[[str], None]] = None,
               on_back: Optional[Callable[[OnlineCover, bytes], None]] = None,
               cancelled: Callable[[], bool] = lambda: False, max_total: int = 24) -> int:
    """Durchsucht die gewaehlten Quellen nacheinander, laedt die Vorschaubilder
    und meldet jedes neue (inhaltlich verschiedene) Bild ueber on_found."""
    seen = set()
    found = 0
    for source in sources:
        if cancelled() or found >= max_total:
            break
        if on_status:
            on_status(f"Suche bei {source} …")
        try:
            if source == SOURCE_WEB:
                candidates = web_covers(page_urls, direct_urls)
            else:
                candidates = SEARCHERS[source](title, artist)
        except Exception as e:
            log.warning("Cover-Suche %s fehlgeschlagen: %s", source, e)
            continue
        for candidate in candidates:
            if cancelled() or found >= max_total:
                break
            data = download_image(candidate.preview_url)
            if not data:
                continue
            digest = hashlib.sha1(data).hexdigest()
            if digest in seen:
                continue
            seen.add(digest)
            found += 1
            if on_found:
                on_found(candidate, data)
            if on_back and candidate.back_preview_url:
                back = download_image(candidate.back_preview_url)
                if back:
                    on_back(candidate, back)
    return found
