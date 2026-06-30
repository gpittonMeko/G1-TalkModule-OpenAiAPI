"""Wake word: Hey G1 e varianti STT (incluso Bepi/Bepì)."""

import pytest

from talk_module.wake import find_wake_and_rest, normalize_wake_stt_text, wake_display_text


@pytest.mark.parametrize(
    "text,expected_kind,expected_rest",
    [
        ("Hey G1", "ack", ""),
        ("Ehi G1", "ack", ""),
        ("Hey G1.", "ack", ""),
        ("hey g one", "ack", ""),
        ("ehi gi uno", "ack", ""),
        ("sei di uno", "ack", ""),
        ("hai g1", "ack", ""),
        ("mark one", "ack", ""),
        ("g1", "ack", ""),
        ("G uno", "ack", ""),
        ("hey j1", "ack", ""),
        ("hey g-1", "ack", ""),
        ("Hey Bepi", "ack", ""),
        ("Ehi Bepi", "ack", ""),
        ("Hey Bepì", "ack", ""),
        ("Ehi Bepì.", "ack", ""),
        ("hey be pi", "ack", ""),
        ("hey bepee", "ack", ""),
        ("hey pepi", "ack", ""),
        ("hey gipi", "ack", ""),
        ("hey jeepy", "ack", ""),
        ("hey gepi", "ack", ""),
        ("hey beppy", "ack", ""),
        ("hey bepi come stai", "ok", "come stai"),
        ("ehi bepi dimmi l'ora", "ok", "dimmi l'ora"),
        ("ciao mondo", "miss", None),
        ("grazie", "miss", None),
    ],
)
def test_find_wake_and_rest(text, expected_kind, expected_rest):
    rest, kind = find_wake_and_rest(text)
    assert kind == expected_kind
    assert rest == expected_rest


@pytest.mark.parametrize(
    "raw,normalized",
    [
        ("Hey Bepì", "Hey g1"),
        ("Ehi be pi", "Ehi g1"),
        ("bepi", "g1"),
        ("Hey G1", "Hey G1"),
    ],
)
def test_normalize_wake_stt_text(raw, normalized):
    assert normalize_wake_stt_text(raw) == normalized


def test_wake_display_ack():
    assert wake_display_text("Ehi Bepì.", "ack") == "Hey G1"
