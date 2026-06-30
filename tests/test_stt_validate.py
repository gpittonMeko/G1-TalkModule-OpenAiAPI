"""STT validation and quick lookup guards."""

import pytest

from talk_module.quick_lookup import is_quick_lookup_question, quick_lookup
from talk_module.stt.validate import is_garbage_command, is_non_italian_transcript, reject_message_for_bad_stt


def test_reject_spanish():
    assert is_non_italian_transcript("Está explotando.")
    assert reject_message_for_bad_stt("Está explotando.") is not None


def test_reject_garbage_ehi():
    assert is_garbage_command("ehi")
    assert reject_message_for_bad_stt("ehi") is not None


def test_accept_italian_question():
    assert not is_non_italian_transcript("Dove siamo oggi?")
    assert reject_message_for_bad_stt("Dove siamo oggi?") is None


def test_dove_siamo_not_quick_lookup():
    assert not is_quick_lookup_question("Dove siamo oggi?")
    assert quick_lookup("Dove siamo oggi?") is None
