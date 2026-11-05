"""Karten aus der db.json des ESPuino-Mediahub (erfundene Daten)"""

import json

import pytest

from core.cover_archive import CoverArchive
from core.mediahub_cards import (
    MediahubError, album_id_for, build_folder, format_card_id, load_cards, resolve_files,
)


def card(esp, cid, name="", files=(), status="assigned", kind="files", updated="2026-10-01T10:00:00+00:00",
         stream_url=""):
    return f"{esp}/{cid}", {
        "esp_id": esp, "card_id": cid, "status": status, "name": name, "kind": kind, "play_mode": 5,
        "stream_url": stream_url, "files": [{"path": p, "size": 1, "sha256": "x"} for p in files],
        "updated_at": updated,
    }


def write_db(path, *cards, devices=None):
    data = {"settings": {}, "devices": devices or {}, "cards": dict(cards)}
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def test_groups_cards_and_skips_pending_and_webradio(tmp_path):
    db = write_db(
        tmp_path / "db.json",
        card("esp-a", "123045067089", "Zoo-Lieder", ["Kinder/Zoo/01.mp3", "Kinder/Zoo/02.mp3"]),
        card("esp-b", "123045067089", "Zoo-Lieder", ["Kinder/Zoo/01.mp3", "Kinder/Zoo/02.mp3"]),
        card("esp-a", "200000000001", status="pending"),
        card("esp-a", "200000000002", "Radio", kind="webradio", stream_url="http://radio.invalid"),
        card("esp-a", "200000000003", "Leer"),
        card("esp-b", "100000000009", "Abendlieder", ["Abend/a.mp3"]),
        devices={"esp-a": {"alias": "Kinderzimmer"}, "esp-b": {"alias": ""}},
    )
    data = load_cards(str(db))
    assert [c.label for c in data.cards] == ["Abendlieder", "Zoo-Lieder"]
    zoo = data.cards[1]
    assert [d.label for d in zoo.devices] == ["Kinderzimmer", "esp-b"]
    assert not zoo.conflict
    assert (data.pending, data.skipped_webradio, data.skipped_empty) == (1, 1, 1)
    assert zoo.album_id == "mediahub123045067089"


def test_data_folder_is_accepted(tmp_path):
    write_db(tmp_path / "db.json", card("esp", "000000000001", "A", ["a.mp3"]))
    assert len(load_cards(str(tmp_path)).cards) == 1


def test_newest_assignment_wins_and_conflict_is_flagged(tmp_path):
    db = write_db(
        tmp_path / "db.json",
        card("esp-a", "111111111111", "Alt", ["alt.mp3"], updated="2026-09-01T10:00:00+00:00"),
        card("esp-b", "111111111111", "Neu", ["neu.mp3"], updated="2026-10-01T10:00:00+00:00"),
    )
    (only,) = load_cards(str(db)).cards
    assert (only.name, only.files, only.source_esp, only.conflict) == ("Neu", ["neu.mp3"], "esp-b", True)


@pytest.mark.parametrize("content", ["{nicht json", "[]", '{"cards": []}'])
def test_unreadable_db_gives_clear_error(tmp_path, content):
    (tmp_path / "db.json").write_text(content, encoding="utf-8")
    with pytest.raises(MediahubError):
        load_cards(str(tmp_path / "db.json"))


def test_missing_db_gives_clear_error(tmp_path):
    with pytest.raises(MediahubError, match="Keine db.json"):
        load_cards(str(tmp_path))


def test_resolve_files_counts_missing_and_blocks_escape(tmp_path):
    media = tmp_path / "media"
    (media / "Kinder").mkdir(parents=True)
    (media / "Kinder" / "01.mp3").write_bytes(b"x")
    (tmp_path / "geheim.mp3").write_bytes(b"x")
    db = write_db(tmp_path / "db.json", card("esp", "000000000001", "A",
                                             ["Kinder/01.mp3", "Kinder/fehlt.mp3", "../geheim.mp3", "/etc/x.mp3"]))
    (c,) = load_cards(str(db)).cards
    found, missing = resolve_files(c, str(media))
    assert found == [str((media / "Kinder" / "01.mp3").resolve())]
    assert missing == 3
    assert resolve_files(c, "") == ([], 4)


def test_build_folder_uses_archive_card_dir(tmp_path):
    media = tmp_path / "media"
    (media / "Hörspiel" / "Folge 1").mkdir(parents=True)
    for name in ("01.mp3", "02.mp3"):
        (media / "Hörspiel" / "Folge 1" / name).write_bytes(b"x")
    db = write_db(tmp_path / "db.json", card("esp", "001002003004", "Folge 1",
                                             ["Hörspiel/Folge 1/01.mp3", "Hörspiel/Folge 1/02.mp3"]))
    (c,) = load_cards(str(db)).cards
    archive = CoverArchive(str(tmp_path / "daten"))
    folder = build_folder(c, str(media), archive)
    assert folder.index is None and folder.name == "Folge 1"
    assert folder.path == str((media / "Hörspiel" / "Folge 1").resolve())
    assert [t.filename for t in folder.tracks] == ["01.mp3", "02.mp3"]
    assert folder.card_dir == str(archive.card_dir("mediahub001002003004"))
    assert folder.card_path == ""

    from pathlib import Path
    Path(folder.card_dir).mkdir(parents=True)
    (Path(folder.card_dir) / "card.png").write_bytes(b"png")
    assert build_folder(c, str(media), archive).card_path.endswith("card.png")


def test_play_mode_label():
    from core.mediahub_cards import play_mode_label
    assert play_mode_label("3") == "Hörbuch (merkt sich die Stelle)"
    assert play_mode_label("99") == "Modus 99" and play_mode_label("x") == "x"


def test_album_id_and_card_id_format():
    assert album_id_for("012345678901") == "mediahub012345678901"
    with pytest.raises(MediahubError):
        album_id_for("../x")
    assert format_card_id("012345678901") == "012 345 678 901"
    assert format_card_id("abc") == "abc"
