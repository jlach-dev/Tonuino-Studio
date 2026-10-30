"""
Tags speichern, ohne die ID3-Version der Datei zu aendern

mutagen schreibt ID3 standardmaessig als Version 2.4. Fuer Dateien der eigenen
Musiksammlung ist das ein stiller Formatwechsel: aeltere Autoradios, Player und
Windows-Explorer/Mediaplayer lesen v2.4 teils nicht. Dateien mit ID3v2.3
bleiben deshalb v2.3; Dateien ohne bisherige Tags bekommen mutagens Standard.
"""


def save_audio(audio) -> None:
    """audio.save() mit unveraenderter ID3-Version (v2.3 bleibt v2.3)"""
    from mutagen.id3 import ID3
    tags = getattr(audio, "tags", None)
    if isinstance(tags, ID3) and tuple(tags.version[:2]) == (2, 3):
        audio.save(v2_version=3)
    else:
        audio.save()
