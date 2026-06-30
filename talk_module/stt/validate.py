"""Validate STT output before LLM / robot routing (Italian path)."""

import re

from talk_module.wake import find_wake_and_rest, is_stt_hallucination, normalize_wake_stt_text

# Whisper allucina spesso in spagnolo su silenzio/rumore.
_SPANISH_MARKERS = (
    "está",
    "esta ",
    "estás",
    "hola",
    "gracias",
    "buenos",
    "buenas",
    "señor",
    "senor",
    "señora",
    "por favor",
    "adiós",
    "adios",
    "qué tal",
    "que tal",
    "cómo",
    "como estas",
    "explota",
    "explotando",
    "tienes",
    "puedes",
    "muchas gracias",
)

# Comandi troppo corti o solo eco della wake word.
_GARBAGE_COMMANDS = frozenset({
    "ehi", "ei", "hey", "hai", "g1", "g one", "g uno", "gi uno", "gi one",
    "bepi", "bepì", "be pi", "pepi", "ok", "sì", "si", "sí",
})

_ENGLISH_ONLY_COMMAND = re.compile(
    r"^(yes|no|okay|yeah|hello|hi|thanks|thank you|what|why|how|who|where|when)\s*[.!?]?$",
    re.IGNORECASE,
)


def is_non_italian_transcript(text: str) -> bool:
    """True se la trascrizione sembra spagnolo/inglese spurio (non italiano utile)."""
    if not text or not text.strip():
        return True
    low = text.strip().lower()
    if is_stt_hallucination(text):
        return True
    for m in _SPANISH_MARKERS:
        if m in low:
            return True
    if _ENGLISH_ONLY_COMMAND.match(low):
        return True
    # Poche parole con caratteri tipici spagnolo e nessuna parola italiana comune.
    if re.search(r"[ñ¿¡]", low):
        return True
    return False


def is_garbage_command(text: str) -> bool:
    """True se dopo la wake non c'è un comando utile."""
    if not text or not text.strip():
        return True
    t = normalize_wake_stt_text(text.strip()).lower().rstrip(".!? ")
    if len(t) < 4:
        return True
    if t in _GARBAGE_COMMANDS:
        return True
    rest, kind = find_wake_and_rest(t)
    if kind == "ack":
        return True
    return False


def reject_message_for_bad_stt(text: str) -> str | None:
    """Messaggio utente se STT non affidabile; None se ok."""
    if is_non_italian_transcript(text):
        return "Non ho capito bene. Ripeti in italiano, per favore."
    if is_garbage_command(text):
        return "Dimmi la domanda dopo «Hey G1», per favore."
    return None
