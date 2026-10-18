import base64
import io
import subprocess
from types import SimpleNamespace

import pytest
from PIL import Image

from core import album_identity
from core.card_layout import CardSpec, ORIENTATION_LANDSCAPE, ORIENTATION_PORTRAIT, prepare_card_art
from core.cover_archive import (
    KIND_AI, KIND_MUSICBRAINZ, ROLE_ALBUM_COVER, ROLE_CARD_COVER, CoverArchive,
)
from core.cover_generator import MAX_REFERENCE_IMAGES, SOURCE_AI, GenerationRequest, generate_art
from core.cover_research import CoverBrief, FolderInfo


def png_bytes(size=(64, 64), color=(255, 0, 0)):
    buffer = io.BytesIO()
    Image.new("RGB", size, color).save(buffer, "PNG")
    return buffer.getvalue()


def jpeg_bytes(size=(64, 64)):
    buffer = io.BytesIO()
    Image.new("RGB", size, (0, 0, 255)).save(buffer, "JPEG")
    return buffer.getvalue()


@pytest.fixture
def silent_mp3(tmp_path):
    """Echte (kurze, stille) MP3 - mutagen braucht gueltige MPEG-Frames"""
    import imageio_ffmpeg
    path = tmp_path / "001.mp3"
    subprocess.run([imageio_ffmpeg.get_ffmpeg_exe(), "-loglevel", "error", "-f", "lavfi", "-i",
                    "anullsrc=r=22050:cl=mono", "-t", "0.3", "-q:a", "9", str(path)], check=True)
    return path


def test_archive_detects_duplicates_by_image_data_not_source(tmp_path):
    archive = CoverArchive(str(tmp_path))
    archive.ensure_album("a1", "Hexe")
    first = archive.add_cover("a1", png_bytes(color=(255, 0, 0)), "local", source="Früheres Album-Cover")
    # Zweites, anderes Bild mit gleicher Herkunft: muss ebenfalls archiviert werden
    second = archive.add_cover("a1", png_bytes(color=(0, 255, 0)), "local", source="Früheres Album-Cover")
    assert second.id != first.id
    assert len(archive.covers("a1")) == 2
    # Gleiche Bilddaten unter anderer Herkunft: kein Duplikat
    again = archive.add_cover("a1", png_bytes(color=(255, 0, 0)), KIND_MUSICBRAINZ, source="https://x/1.png")
    assert again.id == first.id and len(archive.covers("a1")) == 2
    # Gleiches PIL-Bild zweimal
    image = Image.new("RGB", (40, 60), (1, 2, 3))
    assert archive.add_cover("a1", image, KIND_AI).id == archive.add_cover("a1", image, KIND_AI).id

    # Eintraege aus aelteren Versionen ohne gespeicherten Hash: aus der Datei nachberechnen
    import json
    covers_json = tmp_path / "albums" / "a1" / "covers.json"
    data = json.loads(covers_json.read_text(encoding="utf-8"))
    for item in data:
        item.pop("data_sha1")
    covers_json.write_text(json.dumps(data), encoding="utf-8")
    assert archive.add_cover("a1", png_bytes(color=(0, 255, 0)), "local").id == second.id
    assert len(archive.covers("a1")) == 3


def test_archive_add_roles_and_delete(tmp_path):
    archive = CoverArchive(str(tmp_path))
    archive.ensure_album("abc", "Die kleine Hexe", "Preußler", path="E:/01")

    ai = archive.add_cover("abc", Image.new("RGB", (100, 150)), KIND_AI, style="Aquarell", model="m")
    mb = archive.add_cover("abc", jpeg_bytes(), KIND_MUSICBRAINZ, source="https://caa/1.jpg")
    # gleiche Quelle wird nicht doppelt abgelegt
    assert archive.add_cover("abc", jpeg_bytes(), KIND_MUSICBRAINZ, source="https://caa/1.jpg").id == mb.id
    assert ai.file.endswith(".png") and mb.file.endswith(".jpg")
    assert (ai.width, ai.height) == (100, 150)

    archive.set_role("abc", ai.id, ROLE_CARD_COVER)
    archive.set_role("abc", mb.id, ROLE_CARD_COVER)  # exklusiv: geht von ai auf mb ueber
    archive.set_role("abc", ai.id, ROLE_ALBUM_COVER)
    roles = {e.id: e.roles for e in archive.covers("abc")}
    assert roles[ai.id] == [ROLE_ALBUM_COVER]
    assert roles[mb.id] == [ROLE_CARD_COVER]

    archive.delete_cover("abc", ai.id)
    assert [e.id for e in archive.covers("abc")] == [mb.id]
    assert archive.load_image("abc", archive.covers("abc")[0]).size == (64, 64)


def test_archive_finds_album_by_title_and_keeps_paths(tmp_path):
    archive = CoverArchive(str(tmp_path))
    archive.ensure_album("a1", "Die kleine Hexe", "Preußler", path="E:/01")
    archive.ensure_album("a1", path="F:/07")
    assert archive.load_album("a1").paths == ["E:/01", "F:/07"]
    assert archive.find_album("die kleine hexe").id == "a1"
    assert archive.find_album("Die kleine Hexe", "Anderer") is None
    with pytest.raises(ValueError):
        archive.load_album("../x") or archive._album_dir("../x")


def test_album_id_tag_roundtrip(silent_mp3):
    assert album_identity.read_album_id([str(silent_mp3)]) is None
    assert album_identity.write_album_id([str(silent_mp3)], "id-123") == 1
    assert album_identity.read_album_id([str(silent_mp3)]) == "id-123"


def test_prepare_card_art_blurs_square_but_keeps_fitting_images():
    square = prepare_card_art(Image.new("RGB", (500, 500)), ORIENTATION_PORTRAIT)
    assert square.size == CardSpec().size_px(ORIENTATION_PORTRAIT)
    fitting = Image.new("RGB", (1500, 1000))
    assert prepare_card_art(fitting, ORIENTATION_LANDSCAPE).size == (1500, 1000)


def test_generator_sends_all_selected_references_up_to_limit():
    image = SimpleNamespace(data=base64.b64encode(png_bytes((20, 30))).decode())
    calls = []
    client = SimpleNamespace(interactions=SimpleNamespace(
        create=lambda **kw: calls.append(kw) or SimpleNamespace(output_image=image, output_text="")))
    refs = [png_bytes(color=(i, 0, 0)) for i in range(MAX_REFERENCE_IMAGES + 2)]
    req = GenerationRequest(info=FolderInfo(album="A"), brief=CoverBrief(), reference_images=refs)

    generate_art(SOURCE_AI, req, client=client, model="m")

    model_input = calls[0]["input"]
    assert len(model_input) == 1 + MAX_REFERENCE_IMAGES
    assert f"{MAX_REFERENCE_IMAGES} attached reference images" in model_input[0]["text"]
