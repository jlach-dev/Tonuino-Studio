import base64
import io
from types import SimpleNamespace

import pytest
from PIL import Image

from core import cover_research
from core.card_layout import ORIENTATION_LANDSCAPE, ORIENTATION_PORTRAIT
from core.cover_generator import (
    SOURCE_AI, SOURCE_BLUR, GenerationRequest, build_prompt, generate_art,
)
from core.cover_research import CoverBrief, FolderInfo


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


def test_prompt_forbids_text_and_mentions_reference():
    req = GenerationRequest(
        info=FolderInfo(album="Der Grüffelo", artist="Axel Scheffler"),
        brief=CoverBrief(motifs=["Maus", "Wald"], reference_image=b"x"),
        orientation=ORIENTATION_LANDSCAPE,
    )
    prompt = build_prompt(req)
    assert "no text" in prompt
    assert "3:2" in prompt
    assert "Maus" in prompt
    assert "reference" in prompt


def test_generate_ai_art_sends_aspect_ratio_and_reference():
    image = SimpleNamespace(data=base64.b64encode(png_bytes((200, 300))).decode())
    client = fake_client(SimpleNamespace(output_image=image, output_text=""))
    req = GenerationRequest(info=FolderInfo(album="A"), brief=CoverBrief(reference_image=png_bytes()))

    art = generate_art(SOURCE_AI, req, client=client, model="m")

    call = client.interactions.calls[0]
    assert call["response_format"]["aspect_ratio"] == "2:3"
    assert call["input"][1]["mime_type"] == "image/png"
    assert art.size == (200, 300)


def test_generate_ai_art_reports_missing_image():
    client = fake_client(SimpleNamespace(output_image=None, output_text="Safety block"))
    req = GenerationRequest(info=FolderInfo(), brief=CoverBrief(), use_reference=False)
    with pytest.raises(RuntimeError, match="Safety block"):
        generate_art(SOURCE_AI, req, client=client, model="m")


def test_blur_needs_reference_and_works_without_api():
    req = GenerationRequest(info=FolderInfo(), brief=CoverBrief(), orientation=ORIENTATION_PORTRAIT)
    with pytest.raises(ValueError):
        generate_art(SOURCE_BLUR, req)
    req.brief.reference_image = png_bytes()
    assert generate_art(SOURCE_BLUR, req).size[1] > generate_art(SOURCE_BLUR, req).size[0]


def test_research_parses_json_and_prefers_local_cover(monkeypatch):
    text = 'Hier: ```json\n{"summary": "Eine Maus", "motifs": ["Maus"], "palette": ["grün"]}\n```'
    response = SimpleNamespace(output_text=text, model_dump=lambda: {"steps": [{"url": "https://example.org/a"}]})
    monkeypatch.setattr(cover_research, "lookup_online_cover", lambda info: pytest.fail("keine Online-Suche"))

    brief = cover_research.research(FolderInfo(album="X", cover_bytes=b"local"), fake_client(response), "m")

    assert brief.summary == "Eine Maus"
    assert brief.motifs == ["Maus"]
    assert brief.sources == ["https://example.org/a"]
    assert brief.reference_source == "lokal"
    assert "Motive: Maus" in brief.as_text()


def test_research_without_client_uses_online_cover(monkeypatch):
    monkeypatch.setattr(cover_research, "lookup_online_cover", lambda info: b"online")
    brief = cover_research.research(FolderInfo(album="X"))
    assert brief.reference_image == b"online"
    assert brief.reference_source == "Cover Art Archive"
