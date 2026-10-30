import os
import socket
import sys
from pathlib import Path

import pytest

# Qt-Tests (gui.*) brauchen keinen Bildschirm
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

LOOPBACK = {"127.0.0.1", "::1", "localhost"}
SECRET_ENV_VARS = ("GEMINI_API_KEY", "GOOGLE_API_KEY", "SPOTIFY_CLIENT_ID", "SPOTIFY_CLIENT_SECRET",
                   "ACOUSTID_API_KEY")


@pytest.fixture(autouse=True)
def isolate_from_outside_world(monkeypatch):
    """Kein Test spricht mit Netz, Schluesselbund, RFID-Leser, TonUINO oder
    Updater - sonst haengen Ergebnisse von Netz, Hardware und den Zugangsdaten
    des Entwicklers ab. Tests, die z.B. urlopen selbst ersetzen, ueberschreiben
    diese Sperren mit ihrem eigenen monkeypatch."""
    real_connect = socket.socket.connect

    def guarded_connect(sock, address):
        host = address[0] if isinstance(address, tuple) else address
        if host not in LOOPBACK:
            raise OSError(f"Netzwerkzugriff in Tests gesperrt (conftest.py): {address}")
        return real_connect(sock, address)

    monkeypatch.setattr(socket.socket, "connect", guarded_connect)
    for name in SECRET_ENV_VARS:
        monkeypatch.delenv(name, raising=False)

    try:
        import keyring
    except ImportError:
        pass
    else:
        store = {}
        monkeypatch.setattr(keyring, "get_password", lambda service, user: store.get((service, user)))
        monkeypatch.setattr(keyring, "set_password",
                            lambda service, user, value: store.__setitem__((service, user), value))
        monkeypatch.setattr(keyring, "delete_password", lambda service, user: store.pop((service, user), None))

    from core.rfid import RFIDReader
    from core.tonuino_serial import TonuinoSerial
    monkeypatch.setattr(RFIDReader, "get_readers", lambda self: [])
    monkeypatch.setattr(RFIDReader, "connect", lambda self, reader_index=0: False)
    monkeypatch.setattr(TonuinoSerial, "list_ports", staticmethod(lambda: []))

    try:
        from gui.update_controller import UpdateController
    except ImportError:  # ohne PyQt6
        pass
    else:
        monkeypatch.setattr(UpdateController, "check_on_startup", lambda self: None)
