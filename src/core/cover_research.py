"""
Recherche fuer Kartencover: was steckt in einem Ordner, wie koennte das
Motiv aussehen?

Quellen (in dieser Reihenfolge):
1. lokale ID3-Tags (Album, Interpret, Titelliste) und das eingebettete Cover
2. MusicBrainz + Cover Art Archive: Original-Cover, falls lokal keins da ist
   (frei fuer nicht-kommerzielle Nutzung, ohne Key). Regeln laut
   https://musicbrainz.org/doc/MusicBrainz_API/Rate_Limiting: im Mittel max.
   1 Anfrage/s je IP, aussagekraeftiger User-Agent "Name/Version ( Kontakt )",
   bei Ueberschreitung antwortet der Server mit 503.
3. Gemini mit Google-Suche: kurze Inhaltszusammenfassung und Bildideen
"""

import json
import logging
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import List, Optional

from core import __version__
from core.sd_card import Folder

log = logging.getLogger(__name__)

USER_AGENT = f"Tonuino-Manager/{__version__} ( https://github.com/jlach-dev/Tonuino-manager )"
HTTP_TIMEOUT = 15


@dataclass
class FolderInfo:
    """Was lokal ueber einen Ordner bekannt ist"""
    album: str = ""
    artist: str = ""
    titles: List[str] = field(default_factory=list)
    cover_bytes: Optional[bytes] = None


@dataclass
class CoverBrief:
    """Ergebnis der Recherche - Grundlage fuer den Bild-Prompt (im Dialog editierbar)"""
    summary: str = ""
    audience: str = ""
    motifs: List[str] = field(default_factory=list)
    mood: str = ""
    palette: List[str] = field(default_factory=list)
    visual_idea: str = ""
    reference_image: Optional[bytes] = None
    reference_source: str = ""  # "lokal", "Cover Art Archive"
    sources: List[str] = field(default_factory=list)

    def as_text(self) -> str:
        """Menschenlesbare Fassung fuer das Bearbeitungsfeld im Dialog"""
        parts = []
        if self.summary:
            parts.append(f"Inhalt: {self.summary}")
        if self.audience:
            parts.append(f"Zielgruppe: {self.audience}")
        if self.motifs:
            parts.append(f"Motive: {', '.join(self.motifs)}")
        if self.mood:
            parts.append(f"Stimmung: {self.mood}")
        if self.palette:
            parts.append(f"Farben: {', '.join(self.palette)}")
        if self.visual_idea:
            parts.append(f"Bildidee: {self.visual_idea}")
        return "\n".join(parts)


def gather_folder_info(folder: Folder, metadata_manager, max_titles: int = 25) -> FolderInfo:
    """Liest Album/Interpret/Titel aus den Tags und das erste eingebettete Cover"""
    info = FolderInfo()
    for track in folder.tracks[:max_titles]:
        metadata, cover = metadata_manager.read_metadata_and_cover(track.filepath)
        info.album = info.album or metadata.album
        info.artist = info.artist or metadata.album_artist or metadata.artist
        if metadata.title:
            info.titles.append(metadata.title)
        if info.cover_bytes is None and cover:
            info.cover_bytes = cover
    if info.cover_bytes is None and folder.cover_path:
        try:
            with open(folder.cover_path, "rb") as f:
                info.cover_bytes = f.read()
        except OSError:
            pass
    return info


# --- MusicBrainz / Cover Art Archive -------------------------------------------------

_last_mb_request = 0.0
# Mehrere Worker (Dialog, Batch) koennen gleichzeitig anfragen - die
# Drosselung muss deshalb prozessweit gelten
_mb_lock = threading.Lock()
# (album, artist) -> Cover-Bytes oder None: dieselbe Suche nicht wiederholen
_cover_cache: dict = {}


def _http_get(url: str, accept: str = "application/json", attempts: int = 3) -> bytes:
    """GET mit MusicBrainz-Drosselung (>= 1 s Abstand); 503 heisst dort
    "zu schnell" und wird mit wachsender Pause wiederholt."""
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": accept})
    with _mb_lock:
        return _throttled_open(request, attempts)


def _throttled_open(request, attempts: int) -> bytes:
    global _last_mb_request
    for attempt in range(attempts):
        wait = 1.1 * (attempt + 1) - (time.monotonic() - _last_mb_request)
        if wait > 0:
            time.sleep(wait)
        try:
            with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT) as response:
                return response.read()
        except urllib.error.HTTPError as e:
            if e.code != 503 or attempt == attempts - 1:
                raise
        finally:
            _last_mb_request = time.monotonic()
    raise RuntimeError("unreachable")


def _lucene_escape(text: str) -> str:
    return re.sub(r'([+\-!(){}\[\]^"~*?:\\/]|&&|\|\|)', r"\\\1", text)


def find_release_groups(album: str, artist: str = "") -> List[dict]:
    """Passende MusicBrainz-Release-Groups zu Album (+ Interpret), beste zuerst"""
    if not album.strip():
        return []
    query = f'releasegroup:"{_lucene_escape(album)}"'
    if artist.strip():
        query += f' AND artist:"{_lucene_escape(artist)}"'
    url = "https://musicbrainz.org/ws/2/release-group/?" + urllib.parse.urlencode(
        {"query": query, "fmt": "json", "limit": 5})
    data = json.loads(_http_get(url))
    return [g for g in data.get("release-groups", []) if int(g.get("score", 0)) >= 85]


def fetch_cover_art(release_group_id: str) -> Optional[bytes]:
    """Front-Cover (500 px) aus dem Cover Art Archive"""
    url = f"https://coverartarchive.org/release-group/{release_group_id}/front-500"
    try:
        return _http_get(url, accept="image/*")
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise


def lookup_online_cover(info: FolderInfo) -> Optional[bytes]:
    """Original-Cover ueber MusicBrainz/Cover Art Archive. Ergebnisse (auch
    "nichts gefunden") werden gemerkt, Netzwerkfehler nicht."""
    key = (info.album.strip().lower(), info.artist.strip().lower())
    if key in _cover_cache:
        return _cover_cache[key]
    try:
        groups = find_release_groups(info.album, info.artist)
        if not groups and info.artist:
            groups = find_release_groups(info.album)
        cover = None
        # nicht jede Release-Group hat ein Cover im Archiv - die naechsten probieren
        for group in groups[:3]:
            cover = fetch_cover_art(group["id"])
            if cover:
                break
    except Exception:
        log.exception("Cover-Suche bei MusicBrainz fehlgeschlagen")
        return None
    _cover_cache[key] = cover
    return cover


# --- Gemini mit Google-Suche ------------------------------------------------------------

RESEARCH_PROMPT = """Du hilfst, ein Cover für eine RFID-Karte einer Kinder-Musikbox (TonUINO) zu gestalten.
Recherchiere kurz im Internet, worum es in diesem Album/Hörspiel geht.

Album: {album}
Interpret: {artist}
Titel (Auszug): {titles}
Hinweis des Nutzers: {extra}

Antworte NUR mit einem JSON-Objekt (Deutsch) mit diesen Feldern:
"summary": Inhalt in 1-2 Sätzen,
"audience": Zielgruppe/Alter,
"motifs": Liste mit 3-6 konkreten, kindgerechten Bildmotiven (Figuren, Orte, Gegenstände),
"mood": Stimmung in wenigen Worten,
"palette": Liste mit 2-4 passenden Farben,
"visual_idea": eine Bildidee in einem Satz (ein klares Hauptmotiv, gut erkennbar auch in 5 cm Größe).
Wenn du nichts Verlässliches findest, leite die Felder aus Titel und Titelliste ab."""


def _extract_json(text: str) -> dict:
    match = re.search(r"\{.*\}", text or "", re.DOTALL)
    if not match:
        raise ValueError("Antwort enthielt kein JSON")
    return json.loads(match.group(0))


def _collect_urls(obj, found: List[str]):
    """Sucht rekursiv nach Quellen-URLs in der Antwort (Google-Suche-Schritte)"""
    if isinstance(obj, dict):
        for key, value in obj.items():
            if key in ("url", "uri") and isinstance(value, str) and value.startswith("http"):
                if value not in found:
                    found.append(value)
            else:
                _collect_urls(value, found)
    elif isinstance(obj, list):
        for value in obj:
            _collect_urls(value, found)


def make_client(api_key: str):
    from google import genai
    return genai.Client(api_key=api_key)


def research_with_gemini(client, model: str, info: FolderInfo, extra: str = "") -> CoverBrief:
    prompt = RESEARCH_PROMPT.format(
        album=info.album or "(unbekannt)",
        artist=info.artist or "(unbekannt)",
        titles="; ".join(info.titles[:15]) or "(keine)",
        extra=extra or "(keiner)",
    )
    interaction = client.interactions.create(model=model, input=prompt, tools=[{"type": "google_search"}])
    data = _extract_json(interaction.output_text)

    brief = CoverBrief(
        summary=str(data.get("summary", "")),
        audience=str(data.get("audience", "")),
        motifs=[str(m) for m in data.get("motifs", [])][:6],
        mood=str(data.get("mood", "")),
        palette=[str(c) for c in data.get("palette", [])][:4],
        visual_idea=str(data.get("visual_idea", "")),
    )
    try:
        _collect_urls(interaction.model_dump(), brief.sources)
    except Exception:
        pass
    brief.sources = brief.sources[:8]
    return brief


def research(info: FolderInfo, client=None, text_model: str = "", extra: str = "",
             online_cover: bool = True) -> CoverBrief:
    """Komplette Recherche. Ohne client (kein API-Key) nur Cover-Suche + Tags."""
    brief = CoverBrief()
    if client is not None:
        try:
            brief = research_with_gemini(client, text_model, info, extra)
        except Exception as e:
            log.exception("Gemini-Recherche fehlgeschlagen")
            brief.summary = f"(Recherche fehlgeschlagen: {e})"

    if info.cover_bytes:
        brief.reference_image, brief.reference_source = info.cover_bytes, "lokal"
    elif online_cover:
        cover = lookup_online_cover(info)
        if cover:
            brief.reference_image, brief.reference_source = cover, "Cover Art Archive"
    return brief
