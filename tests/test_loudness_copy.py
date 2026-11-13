"""Lautstaerke beim Uebertragen auf den ESPuino angleichen (core/loudness_copy.py)
und das Zusammenspiel mit core/espuino_transfer.py - mit einem nachgebauten
FFmpeg, erfundene Daten"""

import os

import pytest

from core import espuino_transfer as tr
from core.loudness import LoudnessResult
from core.loudness_copy import TransferNormalizer


class FakeConverter:
    """Lautheit je Dateiname; "angleichen" schreibt NORM + Verstaerkung + Original"""

    is_available = True

    def __init__(self, levels=None, broken=()):
        self.levels = levels or {}
        self.broken = set(broken)
        self.measured = []

    def measure_loudness(self, filepath):
        name = os.path.basename(filepath)
        self.measured.append(name)
        if name in self.broken:
            raise RuntimeError("kaputt")
        return LoudnessResult(integrated=self.levels.get(name, -16.0))

    def apply_gain(self, input_path, output_path, gain_db, bitrate_kbps=192):
        with open(input_path, "rb") as src, open(output_path, "wb") as dst:
            dst.write(f"NORM{gain_db:+.1f}|".encode() + src.read())


@pytest.fixture
def album(tmp_path):
    folder = tmp_path / "Bibliothek" / "Die Fuchsbande"
    folder.mkdir(parents=True)
    tracks = []
    for name in ("01 Leise.mp3", "02 Passt.mp3", "03 Kaputt.mp3", "04 Anders.m4a"):
        (folder / name).write_bytes(name.encode() * 10)
        tracks.append(str(folder / name))
    return tracks


def test_normalizer_decides_per_file(album):
    converter = FakeConverter({"01 Leise.mp3": -25.0}, broken={"03 Kaputt.mp3"})
    normalizer = TransferNormalizer(converter)
    out = [normalizer(t) for t in album]
    assert out[0] != album[0] and open(out[0], "rb").read().startswith(b"NORM+9.0|")
    assert out[1:] == album[1:]  # passt schon, nicht messbar, kein MP3: das Original
    assert "04 Anders.m4a" not in converter.measured
    assert (normalizer.adjusted, normalizer.unchanged, normalizer.not_mp3, len(normalizer.errors)) == (1, 1, 1, 1)
    assert normalizer.summary() == ("Lautstärke von 1 Datei angeglichen, 1 passte schon, 1 kein MP3, unverändert, "
                                    "1 nicht messbar, unverändert")
    temp_dir = os.path.dirname(out[0])
    normalizer.cleanup()
    assert not os.path.exists(temp_dir)
    assert open(album[0], "rb").read() == b"01 Leise.mp3" * 10  # Bibliothek unveraendert


def test_copy_to_sd_with_normalizer_keeps_library_and_resumes_by_name(album, tmp_path):
    sd = tmp_path / "sd"
    sd.mkdir()
    plan = tr.plan_album(album, "/Fuchsbande")
    normalizer = TransferNormalizer(FakeConverter({"01 Leise.mp3": -25.0}))
    seen = []
    result = tr.copy_to_sd(plan, str(sd), progress=lambda d, t, name: seen.append(name), prepare=normalizer)
    normalizer.cleanup()
    assert (result.copied, result.skipped) == (4, 0)
    assert (sd / "Fuchsbande" / "01 Leise.mp3").read_bytes().startswith(b"NORM+9.0|")
    assert (sd / "Fuchsbande" / "02 Passt.mp3").read_bytes() == b"02 Passt.mp3" * 10
    assert "01 Leise.mp3 – Lautstärke" in seen
    assert open(album[0], "rb").read() == b"01 Leise.mp3" * 10
    # Die Kopie ist anders gross als das Original: beim zweiten Mal zaehlt nur der Name
    assert tr.pending_items(plan, str(sd), by_name=True) == []
    assert [i.target for i in tr.pending_items(plan, str(sd))] == ["/Fuchsbande/01 Leise.mp3"]
    again = tr.copy_to_sd(plan, str(sd), prepare=TransferNormalizer(FakeConverter()))
    assert (again.copied, again.skipped) == (0, 4)


def test_upload_with_normalizer_sends_the_adjusted_copy(album):
    from tests.test_espuino_transfer import FakeExplorer
    fake = FakeExplorer()
    try:
        normalizer = TransferNormalizer(FakeConverter({"01 Leise.mp3": -10.0}))
        result = tr.upload_to_device(tr.plan_album(album, "/Fuchsbande"), fake.host, prepare=normalizer)
        normalizer.cleanup()
        assert result.copied == 4
        assert fake.files["/Fuchsbande/01 Leise.mp3"] == b"NORM-6.0|" + b"01 Leise.mp3" * 10
        assert fake.files["/Fuchsbande/04 Anders.m4a"] == b"04 Anders.m4a" * 10
    finally:
        fake.close()
