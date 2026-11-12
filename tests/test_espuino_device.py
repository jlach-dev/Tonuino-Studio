"""Karten direkt vom ESPuino: REST-Aufrufe gegen einen nachgebauten ESPuino
(lokaler HTTP-Server, erfundene Daten) und Zuordnung zu Dateien auf der SD-Karte"""

import json
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from core import espuino_device as dev

CARD_A = "001002003004"
CARD_B = "100200300400"


class FakeEspuino:
    def __init__(self):
        self.assignments = {
            CARD_A: {"id": CARD_A, "fileOrUrl": "/Hörspiele/Fuchsbande", "playMode": 3,
                     "lastPlayPos": 0, "trackLastPlayed": 0},
            CARD_B: {"id": CARD_B, "fileOrUrl": "/Musik/Lieder/02.mp3", "playMode": 1,
                     "lastPlayPos": 0, "trackLastPlayed": 0},
            "200200200200": {"id": "200200200200", "modId": 110},
            "300300300300": {"id": "300300300300", "fileOrUrl": "http://radio.invalid/stream", "playMode": 8},
            "400400400400": {"id": "400400400400", "fileOrUrl": "http://hub.invalid", "playMode": 18},
        }
        self.posts = []
        self.deletes = []  # Pfade aller DELETE-Anfragen
        self.requests = []  # Pfade aller GET-Anfragen
        self.hostname = "ESPuino-Testzimmer"
        self.files = {}  # "/Ordner/Datei" -> Inhalt, ueber POST /explorer hochgeladen
        self.dirs = {"/Hörspiele"}  # Ordner auf der SD-Karte
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _send(self, code, payload):
                body = json.dumps(payload).encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _query_path(self):
                return urllib.parse.unquote(urllib.parse.urlparse(self.path).query.partition("path=")[2])

            def do_GET(self):
                fake.requests.append(self.path)
                if self.path == "/info":
                    self._send(200, {"software": {"version": "Software-revision: 20261008-DEV", "branch": "dev"},
                                     "sdcard": {"totalBytes": "32000000000", "freeBytes": "31000000000"},
                                     "wifi": {"ip": "192.0.2.24", "rssi": -50}})
                elif self.path == "/settings?section=wifi":
                    self._send(200, {"wifi": {"hostname": fake.hostname, "scanOnStart": True}})
                elif self.path == "/rfid":
                    self._send(200, list(fake.assignments.values()))
                elif self.path.startswith("/explorer"):
                    folder = self._query_path().rstrip("/")
                    entries = [{"name": d.rpartition("/")[2], "dir": True} for d in sorted(fake.all_dirs())
                               if d.rpartition("/")[0] == folder]
                    entries += [{"name": f.rpartition("/")[2]} for f in fake.files if f.rpartition("/")[0] == folder]
                    self._send(200, entries)
                else:
                    self._send(404, {})

            def do_POST(self):
                raw = self.rfile.read(int(self.headers["Content-Length"]))
                if self.path.startswith("/explorer"):
                    fake.files[self._query_path()] = raw
                    self._send(200, {})
                    return
                data = json.loads(raw)
                fake.posts.append(data)
                if self.path != "/rfid" or not data.get("id"):
                    self.send_response(500)
                    self.end_headers()
                    return
                entry = {"id": data["id"], "fileOrUrl": data["fileOrUrl"], "playMode": data["playMode"],
                         "lastPlayPos": 0, "trackLastPlayed": 0}
                fake.assignments[data["id"]] = entry
                self._send(200, entry)

            def do_DELETE(self):
                fake.deletes.append(self.path)
                if self.path.startswith("/explorer"):
                    target = self._query_path().rstrip("/")
                    for f in [f for f in fake.files if f == target or f.startswith(target + "/")]:
                        del fake.files[f]
                    fake.dirs = {d for d in fake.dirs if d != target and not d.startswith(target + "/")}
                    self._send(200, {})  # wie der echte ESPuino: immer 200
                elif self.path.startswith("/rfid?id="):
                    card_id = self.path.partition("=")[2]
                    if fake.assignments.pop(card_id, None) is None:
                        self._send(404, "Tag not exists")
                    else:
                        self._send(200, {})
                else:
                    self._send(404, {})

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.host = f"127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def all_dirs(self):
        result = set(self.dirs)
        for path in list(self.dirs) + list(self.files):
            parts = path.split("/")[1:-1] if path in self.files else path.split("/")[1:]
            for i in range(1, len(parts) + 1):
                result.add("/" + "/".join(parts[:i]))
        return result

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def esp():
    fake = FakeEspuino()
    yield fake
    fake.close()


@pytest.fixture
def sd(tmp_path):
    root = tmp_path / "sd"
    for rel in ("Hörspiele/Fuchsbande/01.mp3", "Hörspiele/Fuchsbande/02.mp3", "Hörspiele/Fuchsbande/cover.jpg",
                "Hörspiele/Fuchsbande/Bonus/03.mp3", "Musik/Lieder/02.mp3"):
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"ID3" + b"\0" * 32)
    return root


def test_normalize_host():
    assert dev.normalize_host(" http://192.168.178.124/ ") == "192.168.178.124"
    assert dev.normalize_host("espuino.local:8080") == "espuino.local:8080"
    assert dev.ws_url("192.168.178.124") == "ws://192.168.178.124/ws"
    for bad in ("", "   ", "a b", "192.168.1.1?x=1"):
        with pytest.raises(dev.DeviceError):
            dev.normalize_host(bad)


def test_info_and_cards(esp, sd):
    info = dev.fetch_info(esp.host)
    assert dev.firmware_label(info) == "20261008-DEV (dev)"
    data = dev.load_cards(esp.host, str(sd))
    assert [c.card_id for c in data.cards] == [CARD_B, CARD_A]  # sortiert nach Name: "02.mp3" vor "Fuchsbande"
    by_id = {c.card_id: c for c in data.cards}
    assert by_id[CARD_A].name == "Fuchsbande"
    assert by_id[CARD_A].files == ["Hörspiele/Fuchsbande/01.mp3", "Hörspiele/Fuchsbande/02.mp3"]
    assert by_id[CARD_A].sd_path == "/Hörspiele/Fuchsbande"
    assert by_id[CARD_A].album_id == "mediahub" + CARD_A  # Cover gehoert zur Karte, wie im Mediahub-Modus
    assert by_id[CARD_B].files == ["Musik/Lieder/02.mp3"]
    assert (data.skipped_function, data.skipped_webradio, data.skipped_mediahub) == (1, 1, 1)


def test_recursive_mode_includes_subfolders(sd):
    files, found = dev.card_files(str(sd), "/Hörspiele/Fuchsbande", 16)
    assert found and files[-1] == "Hörspiele/Fuchsbande/Bonus/03.mp3" and len(files) == 3


def test_missing_folder_or_no_sd_root(sd):
    assert dev.card_files(str(sd), "/Gibt es nicht", 3) == (["Gibt es nicht"], False)
    assert dev.card_files("", "/Hörspiele/Fuchsbande", 3) == (["Hörspiele/Fuchsbande"], False)
    assert dev.card_files(str(sd), "/../draussen", 3)[1] is False


def test_assign_card(esp):
    result = dev.assign_card(esp.host, "555666777888", "/Hörspiele/Neu", 5)
    assert result["fileOrUrl"] == "/Hörspiele/Neu"
    assert esp.posts == [{"id": "555666777888", "fileOrUrl": "/Hörspiele/Neu", "playMode": 5}]
    for args in (("abc", "/x", 3), ("555666777888", "", 3), ("555666777888", "/" + "x" * 300, 3),
                 ("555666777888", "/x", 0)):
        with pytest.raises(dev.DeviceError):
            dev.assign_card(esp.host, *args)
    assert len(esp.posts) == 1  # ungueltige Eingaben erreichen das Geraet nicht


def test_sd_path_from_local(sd):
    assert dev.sd_path_from_local(str(sd), str(sd / "Hörspiele" / "Fuchsbande")) == "/Hörspiele/Fuchsbande"
    with pytest.raises(dev.DeviceError):
        dev.sd_path_from_local(str(sd), str(sd.parent))
    with pytest.raises(dev.DeviceError):
        dev.sd_path_from_local(str(sd), str(sd))


def test_unreachable_device():
    with pytest.raises(dev.DeviceError, match="nicht erreichbar"):
        dev.fetch_assignments("127.0.0.1:9")


def test_card_id_from_uid_matches_the_espuino():
    assert dev.card_id_from_uid("5C429102") == "092066145002"
    assert dev.card_id_from_uid("04A1B2C3D4E5F6") == "004161178195"  # 7-Byte-UID: die ersten vier Bytes
    assert dev.card_id_from_uid("5c:42:91:02") == "092066145002"
    assert dev.card_id_from_uid("0102") == "" and dev.card_id_from_uid("xyz") == ""


def test_parse_backup_file_of_the_espuino(tmp_path):
    text = ("﻿^001002003004^#/Hörspiele/Fuchsbande#0#3#0\r\n"
            "^100200300400^#/Musik/Lieder/02.mp3#14#1#4\n"
            "kaputte Zeile\n^200200200200^#110\n")
    assert dev.parse_backup(text) == [
        {"id": "001002003004", "fileOrUrl": "/Hörspiele/Fuchsbande", "playMode": 3},
        {"id": "100200300400", "fileOrUrl": "/Musik/Lieder/02.mp3", "playMode": 1}]
    (tmp_path / "backup.txt").write_text(text, encoding="utf-8")
    state = dev.fetch_state("127.0.0.1:9", str(tmp_path), connect=False)  # ESPuino aus, SD-Karte im PC
    assert not state.reachable and state.backup_date
    assert state.assignments == {"001002003004": ("/Hörspiele/Fuchsbande", 3),
                                 "100200300400": ("/Musik/Lieder/02.mp3", 1)}
    empty = dev.fetch_state("127.0.0.1:9", str(tmp_path / "fehlt"), connect=False)
    assert not empty.backup_date and empty.assignments == {}


def test_unreadable_sd_card_does_not_crash(tmp_path, monkeypatch):
    """SD-Karte steckt, liefert aber E/A-Fehler (WinError 1117): kein Absturz, Dateien gelten als fehlend"""
    from pathlib import Path
    from core import espuino_device as dev
    (tmp_path / "Conni").mkdir()
    real_stat = Path.stat

    def broken_stat(self, *a, **k):
        if "Conni" in str(self):
            raise OSError(22, "E/A-Gerätefehler")
        return real_stat(self, *a, **k)

    monkeypatch.setattr(Path, "stat", broken_stat)
    files, found = dev.card_files(str(tmp_path), "/Conni", 3)
    assert files == ["Conni"] and found is False
    state = dev.DeviceState(host="")
    state.folders_from_pc = True
    assert dev.folder_exists(state, str(tmp_path), "/Conni") is None

    def broken_iterdir(self):
        raise OSError(22, "E/A-Gerätefehler")

    monkeypatch.setattr(Path, "iterdir", broken_iterdir)
    state = dev.fetch_state("127.0.0.1:9", str(tmp_path))
    assert not state.folders_from_pc and "nicht lesbar" in state.sd_error
