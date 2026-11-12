"""ACR122U: aufgelegte Karten zuverlaessig erkennen (nachgebauter Leser, erfundene UID).

Der echte Leser verhaelt sich so: Wird die Verbindung mit SCARD_UNPOWER_CARD
getrennt (Voreinstellung von pyscard), schaltet er das Feld ab, die Karte geht
in HALT und gilt als "entfernt", bis man sie abhebt und neu auflegt."""

import pytest

pytest.importorskip("smartcard")

from smartcard.scard import SCARD_LEAVE_CARD, SCARD_SHARE_SHARED, SCARD_UNPOWER_CARD

from core.espuino_device import card_id_from_uid
from core.rfid import RFIDReader

# vor den Monkeypatches aus conftest.py (die gelten erst ab dem Test)
ORIGINAL_CONNECT = RFIDReader.connect
ORIGINAL_GET_READERS = RFIDReader.get_readers
READER_NAME = "ACS ACR122 0"
UID = [0x64, 0x55, 0x91, 0x02]
REMOVED = "Es ist keine weitere Kommunikation möglich, da die Smartcard entfernt wurde. (0x80100069)"
GONE = "Failed to transmit with protocol T1. Das angegebene Lesegerät ist momentan nicht verfügbar. (0x80100017)"


class Hardware:
    def __init__(self):
        self.card = False  # liegt eine Karte auf
        self.awake = False  # ist sie ansprechbar (nicht in HALT)
        self.fail_next_transmit = ""
        self.dispositions = []

    def place(self):
        self.card, self.awake = True, True


class FakeConnection:
    def __init__(self, hw: Hardware):
        self.hw = hw
        self.mode = None
        self.disposition = None

    def connect(self, mode=None, protocol=None, disposition=None):
        self.hw.dispositions.append(disposition)
        self.disposition = disposition
        if mode == SCARD_SHARE_SHARED and not (self.hw.card and self.hw.awake):
            raise Exception("Unable to connect: " + REMOVED)
        self.mode = mode

    def disconnect(self):
        if self.mode is not None and self.hw.card and self.disposition in (None, SCARD_UNPOWER_CARD):
            self.hw.awake = False  # Feld aus: Karte schlaeft, gilt als entfernt
        self.mode = None

    def transmit(self, apdu):
        if self.hw.fail_next_transmit:
            message, self.hw.fail_next_transmit = self.hw.fail_next_transmit, ""
            raise Exception(message)
        if self.mode != SCARD_SHARE_SHARED:
            raise Exception("Failed to transmit: direct mode")
        if not (self.hw.card and self.hw.awake):
            raise Exception("Failed to transmit: " + REMOVED)
        if apdu == RFIDReader.CMD_GET_UID:
            return list(UID), 0x90, 0x00
        return [], 0x63, 0x00  # Lesen/Anmelden der TonUINO-Bloecke: nicht programmiert

    def getATR(self):
        return [0x3B, 0x8F, 0x80, 0x01]


class FakeReader:
    def __init__(self, hw):
        self.hw = hw
        self.connections = 0

    def createConnection(self):
        self.connections += 1
        return FakeConnection(self.hw)

    def __str__(self):
        return READER_NAME


@pytest.fixture
def reader(monkeypatch):
    import smartcard.System
    hw = Hardware()
    fake = FakeReader(hw)
    monkeypatch.setattr(smartcard.System, "readers", lambda: [fake])
    monkeypatch.setattr(RFIDReader, "connect", ORIGINAL_CONNECT)
    monkeypatch.setattr(RFIDReader, "get_readers", ORIGINAL_GET_READERS)
    monkeypatch.setattr("core.rfid.time.sleep", lambda _s: None)
    rfid = RFIDReader()
    rfid._scard_available = True
    return rfid, hw, fake


def test_card_placed_while_reader_waits_is_recognised(reader):
    rfid, hw, _fake = reader
    status = rfid.poll()  # Leser verbinden, ohne Karte im Direktmodus
    assert status.reader == "ok" and rfid._direct_mode
    assert rfid.poll().card == "none"
    hw.place()
    status = rfid.poll()
    assert status.card == "ok" and status.uid == "64559102"
    assert card_id_from_uid(status.uid) == "100085145002"
    for _ in range(3):  # bleibt erkannt, solange sie liegt
        assert rfid.poll().uid == "64559102"
    assert hw.dispositions and all(d == SCARD_LEAVE_CARD for d in hw.dispositions)


def test_dead_connection_is_rebuilt(reader):
    rfid, hw, fake = reader
    hw.place()
    rfid.poll()
    assert rfid.poll().uid == "64559102"
    hw.fail_next_transmit = GONE  # Leser kurz weg
    rfid.poll()
    assert rfid._stale
    status = rfid.poll()  # frisch verbunden
    assert status.reader == "ok" and not rfid._stale and fake.connections == 2
    assert rfid.poll().uid == "64559102"


def test_no_card_is_not_a_dead_connection():
    assert not RFIDReader._reader_gone(Exception(REMOVED))
    assert RFIDReader._reader_gone(Exception(GONE))


def test_disconnect_on_exit_powers_the_field_down(reader):
    rfid, hw, _fake = reader
    hw.place()
    rfid.poll()
    connection = rfid._card
    rfid.disconnect()
    assert connection.disposition == SCARD_UNPOWER_CARD and connection.mode is None and rfid._card is None
