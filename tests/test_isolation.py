"""Die Sperren aus conftest.py greifen wirklich"""

import urllib.request

import pytest

from core import ai_settings
from core.rfid import RFIDReader


def test_network_is_blocked():
    with pytest.raises(OSError):
        urllib.request.urlopen("http://example.com", timeout=2)


def test_keyring_is_in_memory_only():
    assert ai_settings.get_api_key() is None
    ai_settings.set_api_key("test-key")
    assert ai_settings.get_api_key() == "test-key"


def test_keyring_is_empty_again_in_next_test():
    assert ai_settings.get_api_key() is None


def test_rfid_reader_sees_no_hardware():
    reader = RFIDReader()
    assert reader.get_readers() == [] and not reader.connect()
