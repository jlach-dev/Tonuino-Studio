import base64
import io
import json
import shutil
import subprocess
from types import SimpleNamespace

import pytest
from PIL import Image

from core.cover_archive import KIND_AI, KIND_ONLINE, ROLE_SERIES_ANCHOR, CoverArchive
from core.cover_generator import (
    MAX_REFERENCE_IMAGES, SOURCE_AI, GenerationRequest, build_back_prompt, build_prompt, generate_art,
    generate_back_art,
)
from core.cover_research import CoverBrief, FolderInfo
from core.series import (
    MAX_ANCHORS, SIDE_BACK, SeriesProfile, SeriesStore, build_character_sheet_prompt, detect_series,
    generate_character_sheet, normalize_series_name, suggest_style_bible,
)


def png_bytes(size=(64, 64), color=(255, 0, 0)):
    buffer = io.BytesIO()
    Image.new("RGB", size, color).save(buffer, "PNG")
    return buffer.getvalue()


class FakeInteractions:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return self.response


def fake_client(response):
    return SimpleNamespace(interactions=FakeInteractions(response))


def image_response(size=(300, 200)):
    return SimpleNamespace(output_image=SimpleNamespace(data=base64.b64encode(png_bytes(size)).decode()),
                           output_text="")


FUCHS_INFO = FolderInfo(album="Folge 39: Fall 77: Die verschleppte Schleppe/Fall 78: Der gefiederte Vergessling",
                        artist="Die Fuchsbande", titles=["Fall 77", "Fall 78"])


# --- Store ----------------------------------------------------------------------------------------

def test_series_store_roundtrip_and_lookup(tmp_path):
    store = SeriesStore(str(tmp_path))
    profile = store.create("Die Fuchsbande", mb_series_id="mb-1", style="Aquarell")
    profile.style_bible = "Figuren: Fuchs Finn …"
    assert profile.add_anchor("album-a", "cover-1")
    assert profile.add_anchor("album-a", "cover-1", SIDE_BACK)
    assert not profile.add_anchor("album-a", "cover-1")  # Dublette
    store.save(profile)

    loaded = store.load(profile.id)
    assert loaded.name == "Die Fuchsbande" and loaded.mb_series_id == "mb-1" and loaded.style == "Aquarell"
    assert loaded.style_bible == "Figuren: Fuchs Finn …"
    assert [(a.album_id, a.cover_id, a.side) for a in loaded.anchors] == [
        ("album-a", "cover-1", "front"), ("album-a", "cover-1", "back")]
    assert loaded.created and loaded.updated
    assert (tmp_path / "series" / profile.id / "series.json").exists()

    store.create("Bibi Blocksberg")
    assert [p.name for p in store.list_all()] == ["Bibi Blocksberg", "Die Fuchsbande"]
    assert store.find_by_name("die fuchsbande · Folge 39").id == profile.id
    assert store.find_by_mb_id("mb-1").id == profile.id
    assert store.find_by_name("Benjamin") is None
    with pytest.raises(ValueError):
        store.load("../x") or store._dir("../x")

    loaded.remove_anchor("album-a", "cover-1", SIDE_BACK)
    assert len(loaded.anchors) == 1
    store.delete(profile.id)
    assert store.load(profile.id) is None


def test_anchor_limit_and_archive_assignment(tmp_path):
    archive = CoverArchive(str(tmp_path))
    archive.ensure_album("a1", "Folge 1", "Die Fuchsbande")
    store = archive.series
    profile = store.create("Die Fuchsbande")
    for i in range(MAX_ANCHORS + 2):
        profile.add_anchor("a1", f"c{i}")
    assert len(profile.anchors) == MAX_ANCHORS

    archive.assign_album("a1", profile.id)
    assert archive.load_album("a1").series_id == profile.id
    assert [a.id for a in archive.albums_in_series(profile.id)] == ["a1"]
    archive.assign_album("a1", "")
    assert archive.albums_in_series(profile.id) == []

    # Anker-Bilder kommen aus dem Archiv, fehlende werden uebersprungen
    front = archive.add_cover("a1", png_bytes(color=(1, 1, 1)), KIND_ONLINE, source="https://x/1")
    archive.set_back("a1", front.id, png_bytes(color=(2, 2, 2)))
    archive.set_role("a1", front.id, ROLE_SERIES_ANCHOR, exclusive=False)
    profile.anchors = []
    profile.add_anchor("a1", front.id)
    profile.add_anchor("a1", front.id, SIDE_BACK)
    profile.add_anchor("a1", "missing")
    images = store.anchor_images(profile, archive)
    assert images == [png_bytes(color=(1, 1, 1)), png_bytes(color=(2, 2, 2))]
    archive.clear_role("a1", front.id, ROLE_SERIES_ANCHOR)
    assert archive.covers("a1")[0].roles == []


# --- Erkennung ------------------------------------------------------------------------------------

def test_detect_series_from_tags_and_musicbrainz():
    assert detect_series(FUCHS_INFO) == ("Die Fuchsbande", "")
    feed = FolderInfo(album="MausHörspiel lang", titles=["Die kleine Hexe (1/2): Heia | Feed",
                                                       "Die kleine Hexe (2/2): Streich | Feed"])
    assert detect_series(feed) == ("Die kleine Hexe", "")
    assert detect_series(FolderInfo()) == ("", "")

    calls = []

    def lookup(rg_id):
        calls.append(rg_id)
        return [{"id": "mb-series", "name": "Die Fuchsbande (Hörspiel)", "number": "39"}]

    assert detect_series(FUCHS_INFO, "rg-1", lookup=lookup) == ("Die Fuchsbande (Hörspiel)", "mb-series")
    assert calls == ["rg-1"]
    # Netzfehler -> Rueckfall auf die Tags
    assert detect_series(FUCHS_INFO, "rg-1", lookup=lambda _rg: (_ for _ in ()).throw(OSError("offline")))[0] == \
        "Die Fuchsbande"
    assert normalize_series_name("Die Fuchsbande · Folge 39") == normalize_series_name("DIE FUCHSBANDE")


# --- Prompt und Referenzen ------------------------------------------------------------------------

def test_front_prompt_contains_style_bible_and_anchors_first():
    anchors = [png_bytes(color=(10, i, 0)) for i in range(2)]
    refs = [png_bytes(color=(20, i, 0)) for i in range(3)]
    req = GenerationRequest(info=FUCHS_INFO, brief=CoverBrief(), reference_images=refs, anchor_images=anchors,
                            series_name="Die Fuchsbande", series_bible="Figuren: Fuchs Finn mit blauer Latzhose")
    assert req.references() == anchors + refs

    prompt = build_prompt(req)
    assert "Series style bible for the series \"Die Fuchsbande\"" in prompt
    assert "Fuchs Finn mit blauer Latzhose" in prompt
    assert "first 2 attached image(s) are the series reference images" in prompt
    assert "exactly the same characters, illustration style and palette as the reference images" in prompt
    assert "remaining 3 attached image(s)" in prompt

    client = fake_client(image_response())
    generate_art(SOURCE_AI, req, client=client, model="m")
    model_input = client.interactions.calls[0]["input"]
    assert [part["type"] for part in model_input] == ["text"] + ["image"] * 5
    sent = [base64.b64decode(part["data"]) for part in model_input[1:]]
    assert sent[:2] == anchors and sent[2:] == refs


def test_anchors_win_over_references_at_limit():
    anchors = [png_bytes(color=(10, i, 0)) for i in range(MAX_ANCHORS)]
    refs = [png_bytes(color=(20, i, 0)) for i in range(MAX_REFERENCE_IMAGES)]
    req = GenerationRequest(info=FUCHS_INFO, brief=CoverBrief(), reference_images=refs, anchor_images=anchors)
    sent = req.references()
    assert len(sent) == MAX_REFERENCE_IMAGES
    assert sent[:MAX_ANCHORS] == anchors
    assert sent[MAX_ANCHORS:] == refs[:MAX_REFERENCE_IMAGES - MAX_ANCHORS]
    # Anker gehen auch ohne Nutzer-Vorlagen mit; ein Anker, der zugleich Vorlage ist, nur einmal
    req2 = GenerationRequest(info=FUCHS_INFO, brief=CoverBrief(reference_image=anchors[0]), anchor_images=anchors[:1])
    assert req2.references() == anchors[:1]
    req3 = GenerationRequest(info=FUCHS_INFO, brief=CoverBrief(), use_reference=False, anchor_images=anchors[:1])
    assert req3.references() == anchors[:1]
    # ohne Serie bleibt der bisherige Hinweis
    assert "attached original cover" in build_prompt(
        GenerationRequest(info=FUCHS_INFO, brief=CoverBrief(reference_image=refs[0])))


def test_back_prompt_and_request_include_series():
    anchors = [png_bytes(color=(10, i, 0)) for i in range(2)]
    front = png_bytes(color=(99, 0, 0))
    req = GenerationRequest(info=FUCHS_INFO, brief=CoverBrief(), anchor_images=anchors, series_name="Die Fuchsbande",
                            series_bible="Farben: Orange, Waldgrün")
    prompt = build_back_prompt(req)
    assert "Farben: Orange, Waldgrün" in prompt
    assert "2 further attached image(s) are the series reference images" in prompt

    client = fake_client(image_response((200, 300)))
    generate_back_art(client, "m", req, front)
    model_input = client.interactions.calls[0]["input"]
    sent = [base64.b64decode(part["data"]) for part in model_input[1:]]
    assert sent == [front] + anchors


# --- Stilbibel und Charakterblatt -----------------------------------------------------------------

def test_suggest_style_bible_sends_anchor_images():
    client = fake_client(SimpleNamespace(output_text="Figuren: Fuchs Finn.\nStil: Kinderbuch.\n"))
    anchors = [png_bytes(color=(i, 0, 0)) for i in range(MAX_ANCHORS + 1)]
    text = suggest_style_bible(client, "t", "Die Fuchsbande", anchors, "Inhalt: Detektive im Wald")
    assert text.startswith("Figuren: Fuchs Finn.")
    call = client.interactions.calls[0]
    assert call["model"] == "t" and "tools" not in call
    parts = call["input"]
    assert parts[0]["type"] == "text" and "Die Fuchsbande" in parts[0]["text"]
    assert "Detektive im Wald" in parts[0]["text"]
    assert len(parts) == 1 + MAX_ANCHORS and all(p["type"] == "image" for p in parts[1:])

    with pytest.raises(RuntimeError):
        suggest_style_bible(fake_client(SimpleNamespace(output_text="")), "t", "X", [], "")
    # ohne Anker: nur Text
    client2 = fake_client(SimpleNamespace(output_text="ok"))
    suggest_style_bible(client2, "t", "X", [], "")
    assert isinstance(client2.interactions.calls[0]["input"], str)


def test_generate_character_sheet_uses_anchors_and_bible():
    client = fake_client(image_response((300, 200)))
    anchors = [png_bytes(color=(5, 5, 5))]
    image = generate_character_sheet(client, "img", "Die Fuchsbande", "Aquarell", "Stil: Aquarell", anchors)
    assert image.size == (300, 200)
    call = client.interactions.calls[0]
    assert call["response_format"]["aspect_ratio"] == "3:2"
    prompt = call["input"][0]["text"]
    assert "character reference sheet" in prompt and "Die Fuchsbande" in prompt
    assert "watercolor" in prompt and "Stil: Aquarell" in prompt
    assert "no text" in prompt
    assert base64.b64decode(call["input"][1]["data"]) == anchors[0]
    assert "attached reference images" not in build_character_sheet_prompt("X", "Aquarell", "", 0)


# --- Batch-Worker ---------------------------------------------------------------------------------

@pytest.fixture
def sd_folder(tmp_path):
    pytest.importorskip("PyQt6")
    import imageio_ffmpeg
    from core.sd_card import SDCard
    folder = tmp_path / "sd" / "01"
    folder.mkdir(parents=True)
    subprocess.run([imageio_ffmpeg.get_ffmpeg_exe(), "-loglevel", "error", "-f", "lavfi", "-i",
                    "anullsrc=r=22050:cl=mono", "-t", "0.3", "-q:a", "9", str(folder / "001.mp3")], check=True)
    shutil.copy(folder / "001.mp3", folder / "002.mp3")
    card = SDCard(str(tmp_path / "sd"))
    card.scan()
    return card.folders[1]


def test_batch_worker_passes_series_to_request_and_assigns_album(tmp_path, sd_folder, monkeypatch):
    from core import cover_generator, cover_research
    from core.metadata import MetadataManager
    from gui.workers import CoverBatchWorker

    archive = CoverArchive(str(tmp_path / "data"))
    profile = archive.series.create("Die Fuchsbande", style="Aquarell")
    profile.style_bible = "Figuren: Fuchs Finn"
    archive.series.save(profile)
    anchors = [png_bytes(color=(7, 7, 7))]

    requests = []
    monkeypatch.setattr(cover_research, "research", lambda info, *a, **k: CoverBrief(summary="x"))
    monkeypatch.setattr(cover_generator, "generate_art",
                        lambda source, req, client=None, model="": requests.append(req) or Image.new("RGB", (400, 600)))

    worker = CoverBatchWorker([sd_folder], MetadataManager(), object(), "t", "img", SOURCE_AI, "Kinderbuch-Illustration",
                              "portrait", archive=archive, series_profile=profile, anchor_images=anchors)
    done = []
    worker.completed.connect(done.append)
    worker.run()

    assert done == [1]
    assert len(requests) == 1
    req = requests[0]
    assert req.series_name == "Die Fuchsbande" and req.series_bible == "Figuren: Fuchs Finn"
    assert req.anchor_images == anchors and req.style == "Aquarell"  # Stil der Serie gewinnt
    albums = archive.all_albums()
    assert len(albums) == 1 and albums[0].series_id == profile.id
    assert [e.style for e in archive.covers(albums[0].id)] == ["Aquarell"]
    meta = json.loads((tmp_path / "sd" / "01" / "card.json").read_text(encoding="utf-8"))
    assert meta["style"] == "Aquarell"
    assert isinstance(KIND_AI, str) and isinstance(SeriesProfile(id="x"), SeriesProfile)
