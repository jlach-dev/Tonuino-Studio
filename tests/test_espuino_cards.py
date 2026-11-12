"""Bekannte ESPuino-Karten: Verzeichnis, Zuordnung zur Bibliothek und Status
gegen den Stand eines Geraets (erfundene Daten, ohne Netz)"""

import json

from core import espuino_cards as ec
from core.espuino_device import DeviceState, build_cards
from core.library import LibraryAlbum, LibraryIndex
from core.mediahub_cards import build_folder

CARD_A = "001002003004"
CARD_B = "100200300400"
CARD_C = "500600700800"


def _library(tmp_path):
    albums = []
    for name, album_id in (("Die Fuchsbande", "fuchs1"), ("Conni: Am Meer", "")):
        folder = tmp_path / "Bibliothek" / name.replace(":", "")
        folder.mkdir(parents=True)
        tracks = []
        for track in ("01.mp3", "02.mp3"):
            (folder / track).write_bytes(b"ID3" + b"\0" * 16)
            tracks.append(str(folder / track))
        albums.append(LibraryAlbum(path=str(folder), title=name, tracks=tracks, album_id=album_id))
    return LibraryIndex(albums=albums)


def test_save_and_load_roundtrip(tmp_path):
    known = ec.load(str(tmp_path))
    assert known.cards == {} and known.path == tmp_path / "espuino" / "cards.json"
    known.remember(CARD_A, name="Fuchsbande", sd_folder="/Die Fuchsbande", play_mode=5, album_id="fuchs1")
    known.save()
    again = ec.load(str(tmp_path))
    card = again.get(CARD_A)
    assert (card.name, card.sd_folder, card.play_mode, card.album_id) == ("Fuchsbande", "/Die Fuchsbande", 5, "fuchs1")
    assert not again.merge_assignment(CARD_A, "/Anders", 3)  # bekannte Karte bleibt
    assert again.merge_assignment(CARD_B, "/Hörspiele/Neu", 3) and again.get(CARD_B).label == "Neu"


def test_unreadable_file_is_never_overwritten(tmp_path):
    path = ec.cards_file(str(tmp_path))
    path.parent.mkdir(parents=True)
    path.write_text("{kaputt", encoding="utf-8")
    known = ec.load(str(tmp_path))
    known.remember(CARD_A, name="x")
    known.save()
    assert path.read_text(encoding="utf-8") == "{kaputt"


def test_find_library_album(tmp_path):
    index = _library(tmp_path)
    by_id = ec.KnownCard(CARD_A, album_id="fuchs1", sd_folder="/irgendwas")
    assert ec.find_library_album(by_id, index).title == "Die Fuchsbande"
    by_folder = ec.KnownCard(CARD_B, sd_folder="/Conni Am Meer")  # Doppelpunkt fehlt auf der SD-Karte
    assert ec.find_library_album(by_folder, index).title == "Conni: Am Meer"
    assert ec.find_library_album(ec.KnownCard(CARD_C, sd_folder="/Gibt es nicht"), index) is None


def test_build_cards_status(tmp_path):
    index = _library(tmp_path)
    known = ec.load(str(tmp_path))
    known.remember(CARD_C, name="Fuchsbande", sd_folder="/Die Fuchsbande", play_mode=3)  # nur bekannt
    state = DeviceState(host="esp", reachable=True, folders=["/Die Fuchsbande"], dirs={"/": {"Die Fuchsbande"}})
    state.assignments = {CARD_A: ("/Die Fuchsbande", 3), CARD_B: ("/Conni Am Meer", 5)}
    data, changed = build_cards(known, state, index, "")
    assert changed
    by_id = {c.card_id: c for c in data.cards}
    assert (by_id[CARD_A].device_status, by_id[CARD_A].folder_present) == (ec.ASSIGNED, True)
    assert (by_id[CARD_B].device_status, by_id[CARD_B].folder_present) == (ec.ASSIGNED, False)
    assert (by_id[CARD_C].device_status, by_id[CARD_C].folder_present) == (ec.NOT_ASSIGNED, True)
    assert by_id[CARD_B].library_path.endswith("Conni Am Meer") and len(by_id[CARD_B].local_tracks) == 2
    assert known.get(CARD_A).album_id == "fuchs1"  # Album-ID gemerkt: findet das Album auch nach Umbenennen
    known.save()
    assert CARD_B in json.loads(ec.cards_file(str(tmp_path)).read_text(encoding="utf-8"))["cards"]

    # Cover ohne SD-Karte im PC: die Dateien kommen aus der Bibliothek
    folder = build_folder(by_id[CARD_B], "", None)
    assert len(folder.tracks) == 2 and folder.missing_files == []


def test_device_reassignment_wins_if_its_folder_exists(tmp_path):
    known = ec.load(str(tmp_path))
    known.remember(CARD_A, name="Alt", sd_folder="/Alt", album_id="x")
    state = DeviceState(host="esp", reachable=True, folders=["/Neu"], dirs={"/": {"Neu"}})
    state.assignments = {CARD_A: ("/Neu", 5)}
    data, changed = build_cards(known, state, None, "")
    assert changed and known.get(CARD_A).sd_folder == "/Neu" and known.get(CARD_A).album_id == ""
    assert data.cards[0].device_status == ec.ASSIGNED

    state.assignments = {CARD_A: ("/Verschwunden", 3)}  # zeigt ins Leere: Studio behaelt seinen Stand
    data, _ = build_cards(known, state, None, "")
    assert known.get(CARD_A).sd_folder == "/Neu" and data.cards[0].device_status == ec.ASSIGNED_OTHER


def test_unreachable_device_has_unknown_status(tmp_path):
    known = ec.load(str(tmp_path))
    known.remember(CARD_A, sd_folder="/X")
    data, _ = build_cards(known, DeviceState(host="esp", error="weg"), None, "")
    assert data.cards[0].device_status == "" and data.cards[0].folder_present is None


def test_card_of_library_album_uses_its_card_cover(tmp_path):
    index = _library(tmp_path)
    known = ec.load(str(tmp_path))
    state = DeviceState(host="esp", reachable=True, folders=["/Die Fuchsbande"], dirs={"/": {"Die Fuchsbande"}})
    state.assignments = {CARD_A: ("/Die Fuchsbande", 3), CARD_C: ("/Unbekannt", 3)}
    data, _ = build_cards(known, state, index, "")
    by_id = {c.card_id: c for c in data.cards}
    assert by_id[CARD_A].album_id == "fuchs1"  # Verweis auf das Album der Bibliothek, keine eigene Kopie
    assert by_id[CARD_C].album_id == "mediahub" + CARD_C  # ohne Album bleibt das Cover an der Karte


def test_recycle_bin_is_no_library_album(tmp_path):
    recycled = tmp_path / "#recycle" / "Conni geht verloren"
    recycled.mkdir(parents=True)
    (recycled / "01.mp3").write_bytes(b"ID3")
    index = _library(tmp_path)
    index.albums.append(LibraryAlbum(path=str(recycled), title="Conni geht verloren",
                                     tracks=[str(recycled / "01.mp3")], album_id="conni"))
    known = ec.load(str(tmp_path))
    known.remember(CARD_B, name="Conni geht verloren", sd_folder="/Conni geht verloren", play_mode=3,
                   library_path=str(recycled), album_id="conni")
    assert ec.find_library_album(known.get(CARD_B), index) is None
    data, changed = build_cards(known, DeviceState(host="esp", reachable=True, folders=[], dirs={"/": set()}),
                                index, "")
    assert changed and known.get(CARD_B).library_path == "" and known.get(CARD_B).album_id == ""
    assert data.cards[0].album_id == "mediahub" + CARD_B and data.cards[0].local_tracks == []
