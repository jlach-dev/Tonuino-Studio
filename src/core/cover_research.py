"""
Recherche fuer Kartencover: was steckt in einem Ordner, wie koennte das
Motiv aussehen?

Quellen (in dieser Reihenfolge):
1. lokale ID3-Tags (Album, Interpret, Titelliste) und das eingebettete Cover
2. MusicBrainz + Cover Art Archive (core/musicbrainz.py): Original-Cover
3. Gemini mit Google-Suche: kurze Inhaltszusammenfassung und Bildideen
"""

import json
import logging
import re
from dataclasses import dataclass, field
from typing import List, Optional

from core import musicbrainz
from core.sd_card import Folder

log = logging.getLogger(__name__)


@dataclass
class FolderInfo:
    """Was lokal ueber einen Ordner bekannt ist"""
    album: str = ""
    artist: str = ""
    titles: List[str] = field(default_factory=list)
    cover_bytes: Optional[bytes] = None
    albums: List[str] = field(default_factory=list)  # alle verschiedenen Album-Tags (mehrere Folgen je Karte)
    album_counts: dict = field(default_factory=dict)  # Album-Tag -> Anzahl Tracks damit
    album_artist: str = ""
    tags_verified: bool = False  # MusicBrainz-IDs in den Tags (siehe core/album_identity.py)


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
    album_title: str = ""  # von der Recherche ermittelter Album-/Folgentitel
    series: str = ""  # Hoerspielreihe, falls erkannt
    cover_urls: List[str] = field(default_factory=list)  # gefundene Bild-URLs des Original-Covers

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
        info.album_artist = info.album_artist or metadata.album_artist
        if metadata.album:
            if metadata.album not in info.albums:
                info.albums.append(metadata.album)
            info.album_counts[metadata.album] = info.album_counts.get(metadata.album, 0) + 1
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
    if folder.tracks:
        from core.album_identity import read_mb_ids
        info.tags_verified = bool(read_mb_ids(folder.tracks[0].filepath)["release_id"])
    return info


_TRACK_SUFFIX = re.compile(r"\s*(\(\d+\s*/\s*\d+\)|[-–:]\s*(teil|part|track|kapitel)\s*\d+.*|(teil|part)\s*\d+)\s*$",
                           re.IGNORECASE)
_TRACK_PREFIX = re.compile(r"^\s*\d{1,3}\s*[-.:)]\s*")


def clean_track_title(title: str) -> str:
    """'01 - Die kleine Hexe (1/2)' -> 'Die kleine Hexe' (fuer die Suche)"""
    text = _TRACK_PREFIX.sub("", (title or "").split(" | ")[0])
    # "Die kleine Hexe (1/2): Heia Walpurgisnacht" - ab der Teil-Angabe folgt nur das Kapitel
    text = re.split(r"\s*\(\d+\s*/\s*\d+\)", text)[0]
    previous = None
    while previous != text:
        previous = text
        text = _TRACK_SUFFIX.sub("", text).strip()
    return text.split(" | ")[0].strip()


@dataclass
class Episode:
    series: str
    number: int
    title: str


# "Die kleine Hexe (1/2): Heia Walpurgisnacht | MausHörspiel lang (07.12.2024)"
_EPISODE_PART = re.compile(
    r"^(?P<series>.+?)\s*\((?P<n>\d+)\s*/\s*(?P<m>\d+)\)\s*[:\-–]\s*(?P<title>.+)$")
# "Bibi Blocksberg - Folge 12: Der Superbesen", "Benjamin Blümchen – Teil 3 - ..."
_EPISODE_WORD = re.compile(
    r"^(?P<series>.+?)\s*[-–:|]\s*(?:folge|teil|episode|kapitel)\s*(?P<n>\d+)\s*[:\-–.]\s*(?P<title>.+)$",
    re.IGNORECASE)


def parse_episode_titles(titles: List[str]) -> List[Episode]:
    """Erkennt in Tracktiteln das Muster "Reihe (n/m): Folgentitel" bzw.
    "Reihe - Folge n: Folgentitel" (typisch fuer Podcast-/Radio-Downloads, bei
    denen der Album-Tag nur den Feed nennt). Liefert nur dann Folgen, wenn alle
    passenden Titel dieselbe Reihe nennen - sonst leer."""
    episodes: List[Episode] = []
    for title in titles:
        text = (title or "").split(" | ")[0].strip()
        text = _TRACK_PREFIX.sub("", text)
        match = _EPISODE_PART.match(text) or _EPISODE_WORD.match(text)
        if match:
            episodes.append(Episode(match.group("series").strip(" -–:"), int(match.group("n")),
                                    match.group("title").strip()))
    if not episodes or len({e.series.lower() for e in episodes}) != 1:
        return []
    # gleiche Folge auf mehreren Tracks (Teil 1/2) nur einmal
    unique: List[Episode] = []
    for episode in episodes:
        if not any(u.number == episode.number and u.title.lower() == episode.title.lower() for u in unique):
            unique.append(episode)
    return unique


def suggest_search_title(info: FolderInfo, brief: Optional[CoverBrief] = None) -> str:
    """Suchbegriff fuer die Cover-Suche - nie der vom Manager vergebene
    Ordnername ("Ordner 01"): aus Tracktiteln erkannte Reihe (der Album-Tag ist
    dann meist nur der Podcast-Feed), sonst Album-Tag, sonst Recherche-Titel,
    sonst der bereinigte erste Tracktitel."""
    episodes = parse_episode_titles(info.titles)
    if episodes:
        return episodes[0].series
    if info.album.strip():
        return info.album.strip()
    if brief is not None and brief.album_title.strip():
        return brief.album_title.strip()
    for title in info.titles:
        cleaned = clean_track_title(title)
        if cleaned:
            return cleaned
    return ""


# --- MusicBrainz / Cover Art Archive (siehe core/musicbrainz.py) -----------------------

# (album, artist) -> Cover-Bytes oder None: dieselbe Suche nicht wiederholen
_cover_cache: dict = {}


def lookup_online_cover(info: FolderInfo) -> Optional[bytes]:
    """Erstbestes Front-Cover ueber MusicBrainz/Cover Art Archive (fuer den
    Batch-Modus; der Cover-Manager zeigt stattdessen alle Kandidaten zur
    Auswahl). Ergebnisse werden gemerkt, Netzwerkfehler nicht."""
    key = (info.album.strip().lower(), info.artist.strip().lower())
    if key in _cover_cache:
        return _cover_cache[key]
    try:
        cover = None
        # nicht jede Release-Group hat ein Cover im Archiv - die naechsten probieren
        for group in musicbrainz.search_release_groups(info.album, info.artist, limit=5, min_score=85)[:3]:
            cover = musicbrainz.fetch_front_cover(group.id)
            if cover:
                break
    except Exception:
        log.exception("Cover-Suche bei MusicBrainz fehlgeschlagen")
        return None
    _cover_cache[key] = cover
    return cover


# --- Gemini mit Google-Suche ------------------------------------------------------------

RESEARCH_PROMPT = """Du hilfst, ein Cover für eine RFID-Karte einer Kinder-Musikbox (TonUINO) zu gestalten.
Recherchiere kurz im Internet, worum es in GENAU diesem Album bzw. dieser Folge geht.

Hörspielreihe / Werk: {series}
Folge(n) auf dieser Karte: {episodes}
Interpret / Autor: {artist}
Album-Tag der Dateien: {album_tag}
Tracktitel (Auszug): {titles}
Hinweis des Nutzers: {extra}

{tag_note}
Wichtig: Es geht ausschließlich um dieses eine Album / diese Folge(n). Andere Alben oder Folgen derselben
Reihe (z.B. Fortsetzungen, Tour- oder Sonderausgaben) gehören NICHT in Inhalt, Motive oder Bildidee.
Prüfe anhand der Tracktitel, ob eine Quelle wirklich dieses Album beschreibt.

Antworte NUR mit einem JSON-Objekt (Deutsch) mit diesen Feldern:
"summary": Inhalt in 1-2 Sätzen,
"audience": Zielgruppe/Alter,
"motifs": Liste mit 3-6 konkreten, kindgerechten Bildmotiven (Figuren, Orte, Gegenstände),
"mood": Stimmung in wenigen Worten,
"palette": Liste mit 2-4 passenden Farben,
"visual_idea": eine Bildidee in einem Satz (ein klares Hauptmotiv, gut erkennbar auch in 5 cm Größe),
"album_title": der offizielle Titel des Albums bzw. der Hörspielfolge (z.B. "Folge 39: Fall 77: ..."),
"series": Name der Hörspielreihe, falls es eine ist, sonst "",
"cover_urls": Liste direkter Bild-URLs (.jpg/.png/.webp) des Original-Covers, die du auf Verlags-, Shop-
oder Streaming-Seiten gefunden hast (nur echte, gefundene URLs, keine erfundenen; sonst leere Liste).
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
    from core.card_text import describe_work  # (card_text importiert dieses Modul)
    work = describe_work(info)
    if work.from_track_titles:
        tag_note = ("Der Album-Tag ist nur der Name eines Podcast-/Radio-Feeds, NICHT der Werktitel - "
                    "recherchiere ausschließlich zu Reihe und Folge(n).")
    elif work.tags_verified:
        tag_note = "Die Tags wurden mit MusicBrainz abgeglichen und sind verlässlich."
    else:
        tag_note = "Reihe/Folge wurden aus den Tags abgeleitet; prüfe sie gegen deine Suchergebnisse."
    prompt = RESEARCH_PROMPT.format(
        series=work.series or work.album_tag or "(unbekannt – aus Titeln und Interpret ableiten)",
        episodes="; ".join(work.episodes) or "(keine Angabe)",
        artist=work.artist or "(unbekannt)",
        album_tag=work.album_tag or "(keiner)",
        titles="; ".join(info.titles[:15]) or "(keine)",
        extra=extra or "(keiner)",
        tag_note=tag_note,
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
        album_title=str(data.get("album_title", "") or ""),
        series=str(data.get("series", "") or ""),
        cover_urls=[str(u) for u in data.get("cover_urls", []) or [] if str(u).startswith("http")][:5],
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
