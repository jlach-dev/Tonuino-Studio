"""Album auf die SD-Karte eines ESPuino: Zielnamen, Kopieren in den
Kartenleser und Hochladen ueber das WLAN (nachgebauter ESPuino, erfundene Daten)"""

import json
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from core import espuino_transfer as tr
from core.espuino_device import DeviceError


class FakeExplorer:
    """Nur /explorer: GET listet, POST schreibt den rohen Inhalt, DELETE loescht"""

    def __init__(self, fail_on: str = ""):
        self.files = {"/Alt/01.mp3": b"alt"}
        self.deleted = []
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _path(self):
                return urllib.parse.unquote(urllib.parse.urlparse(self.path).query.partition("path=")[2])

            def do_GET(self):
                folder = self._path().rstrip("/")
                names = [p.rpartition("/")[2] for p in fake.files if p.rpartition("/")[0] == folder]
                body = json.dumps([{"name": n} for n in names]).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self):
                data = self.rfile.read(int(self.headers["Content-Length"]))
                path = self._path()
                if fail_on and path.endswith(fail_on):
                    fake.files[path] = data[:3]  # halb geschrieben
                    self.send_response(500)
                    self.end_headers()
                    return
                fake.files[path] = data
                self.send_response(200)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def do_DELETE(self):
                path = self._path()
                fake.deleted.append(path)
                fake.files.pop(path, None)
                self.send_response(200)
                self.send_header("Content-Length", "0")
                self.end_headers()

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.host = f"127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def album(tmp_path):
    folder = tmp_path / "Bibliothek" / "Die Fuchsbande"
    folder.mkdir(parents=True)
    tracks = []
    for name in ("01 Anfang.mp3", "02 Mitte.mp3", "03 Ende.mp3"):
        (folder / name).write_bytes(name.encode() * 10)
        tracks.append(str(folder / name))
    return tracks


def test_names():
    assert tr.safe_name('Folge 1: "Wer?" / Teil 2.') == "Folge 1 Wer Teil 2"
    assert tr.normalize_folder("\\Hörspiele\\..\\Fuchsbande ") == "/Hörspiele/Fuchsbande"
    with pytest.raises(DeviceError):
        tr.normalize_folder(" / ")


def test_plan_keeps_names_in_order(album):
    plan = tr.plan_album(album, "/Die Fuchsbande")
    assert [i.target for i in plan.items] == ["/Die Fuchsbande/01 Anfang.mp3", "/Die Fuchsbande/02 Mitte.mp3",
                                              "/Die Fuchsbande/03 Ende.mp3"]
    assert plan.total == sum(i.size for i in plan.items) > 0


def test_plan_numbers_when_names_are_out_of_order(album):
    plan = tr.plan_album([album[2], album[0], album[1]], "Fuchsbande")
    assert [i.target.rpartition("/")[2] for i in plan.items] == ["01 03 Ende.mp3", "02 01 Anfang.mp3",
                                                                  "03 02 Mitte.mp3"]


def test_copy_to_sd_and_resume(album, tmp_path):
    sd = tmp_path / "sd"
    sd.mkdir()
    plan = tr.plan_album(album, "/Hörspiele/Fuchsbande")
    seen = []
    result = tr.copy_to_sd(plan, str(sd), progress=lambda done, total, name: seen.append((done, total)))
    assert (result.copied, result.skipped) == (3, 0)
    assert (sd / "Hörspiele" / "Fuchsbande" / "02 Mitte.mp3").read_bytes() == b"02 Mitte.mp3" * 10
    assert seen[-1] == (plan.total, plan.total)
    again = tr.copy_to_sd(plan, str(sd))
    assert (again.copied, again.skipped) == (0, 3)
    with pytest.raises(DeviceError):
        tr.copy_to_sd(plan, str(tmp_path / "fehlt"))


def test_copy_cancel(album, tmp_path):
    sd = tmp_path / "sd"
    sd.mkdir()
    result = tr.copy_to_sd(tr.plan_album(album, "/X"), str(sd), cancelled=lambda: True)
    assert result.cancelled and result.copied == 0


def test_upload_and_skip_existing(album):
    fake = FakeExplorer()
    try:
        plan = tr.plan_album(album, "/Die Fuchsbande")
        result = tr.upload_to_device(plan, fake.host)
        assert (result.copied, result.skipped) == (3, 0)
        assert fake.files["/Die Fuchsbande/03 Ende.mp3"] == b"03 Ende.mp3" * 10
        assert tr.list_folder(fake.host, "/Die Fuchsbande") == {"01 Anfang.mp3", "02 Mitte.mp3", "03 Ende.mp3"}
        again = tr.upload_to_device(plan, fake.host)
        assert (again.copied, again.skipped) == (0, 3)
        assert fake.deleted == []
    finally:
        fake.close()


def test_failed_upload_removes_only_the_partial_file(album):
    fake = FakeExplorer(fail_on="02 Mitte.mp3")
    try:
        with pytest.raises(DeviceError, match="abgelehnt"):
            tr.upload_to_device(tr.plan_album(album, "/Die Fuchsbande"), fake.host)
        assert fake.deleted == ["/Die Fuchsbande/02 Mitte.mp3"]
        assert "/Die Fuchsbande/01 Anfang.mp3" in fake.files and "/Alt/01.mp3" in fake.files
    finally:
        fake.close()
