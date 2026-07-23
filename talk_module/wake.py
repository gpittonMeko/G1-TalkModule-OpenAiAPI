"""Wake word detection and STT hallucination filtering for continuous listen."""

import re
from typing import Optional

# Phrases Whisper / gpt-4o-transcribe often produce on silence or background noise.
STT_HALLUCINATION_PATTERNS = (
    "sottotitoli",
    "amara.org",
    "amara ",
    "qtss",
    "subtitle",
    "subtitles",
    "created by",
    "a cura di",
    "please subscribe",
    "thanks for watching",
    "thank you for watching",
    "buonasera grazie",
    "grazie per la visione",
    "thank you",
    "goodbye",
    "hello",
    "bye bye",
    "you're welcome",
    "good morning",
    "good evening",
    "good night",
    "see you next time",
    "the end",
    "music playing",
    "music",
    "applause",
    "laughter",
    "silence",
    "foreign",
    "inaudible",
)

WAKE_STT_PROMPT = (
    "Italiano. L'utente potrebbe dire 'Hey G1', 'Ehi G1', 'G1'. "
    "Vocabolario: G1, G One, Gi One, Ehi G1, Hey G1. "
    "Se non senti nulla, rispondi vuoto. Non inventare frasi."
)


def _is_repeated_single_word(text: str) -> bool:
    """True if whitespace-separated tokens are the same word repeated (STT junk)."""
    parts = text.strip().split()
    if len(parts) < 2:
        return False
    cleaned: list[str] = []
    for p in parts:
        core = re.sub(r"^[^\w]+|[^\w]+$", "", p, flags=re.UNICODE)
        if not core:
            return False
        cleaned.append(core.lower())
    return len(set(cleaned)) == 1


_ENGLISH_ONLY_JUNK = {
    "yes", "no", "ok", "okay", "yeah", "yep", "nope", "right", "sure",
    "what", "why", "how", "who", "where", "when", "hi", "hey", "oh",
    "so", "well", "like", "just", "um", "uh", "hmm", "huh",
}


def is_stt_hallucination(text: str) -> bool:
    """True if transcription is empty, a known ghost phrase, punctuation-only, or repeated token spam."""
    if not text or not text.strip():
        return True
    s = text.strip()
    low = s.lower()
    for pat in STT_HALLUCINATION_PATTERNS:
        if pat.lower() in low:
            return True
    if not any(c.isalnum() for c in s):
        return True
    if _is_repeated_single_word(s):
        return True
    core = re.sub(r"[^\w\s]", "", low, flags=re.UNICODE).strip()
    if core in _ENGLISH_ONLY_JUNK:
        return True
    return False


def find_wake_and_rest(t: str) -> tuple[Optional[str], str]:
    """
    Ascolto continuo: rileva wake (Hey G1, G1, G one, gi one, Mark one, …).
    Ritorna (resto dopo wake, kind) con kind: miss | ack | ok.
    rest None se miss; stringa vuota se solo wake; altrimenti testo comando.
    Gestisce varianti Whisper: giuno, gi uno, di uno, gì uno, sei di uno, etc.
    """
    if not t or not t.strip():
        return None, "miss"
    s = t.strip()
    if is_stt_hallucination(s):
        return None, "miss"
    # Varianti STT: g1, g one, gi one, giuno, di uno, gì uno, jee one, ji one, mark one, j1, g-1
    wake_core = (
        r"(?:"
        r"g[\s\-]*1"  # g1, g 1, g-1
        r"|\bg1\b"  # g1
        r"|g[\s\-]*one"  # g one
        r"|gi[\s\-]*one"  # gi one
        r"|giuno"  # giuno (attached)
        r"|gì[\s\-]*one"  # gì one (accented)
        r"|gì[\s\-]*uno"  # gì uno
        r"|gi[\s\-]*uno"  # gi uno
        r"|di[\s\-]*uno"  # di uno (Whisper mishearing)
        r"|ji[\s\-]*one"  # ji one
        r"|jee[\s\-]*one"  # jee one
        r"|mark[\s\-]*one"  # mark one
        r"|markone"  # markone
        r"|j[\s\-]*1"  # j1
        r")"
    )
    # Prefissi wake: hey, ehi, ei, e, sei, hai (Whisper trascrive "hai" per "hey", "sei di uno" per "ehi G1")
    wake_prefix = r"(?:hey|ehi|ei|e|sei|hai)\s*[,.\s]*\s*"
    m = re.search(rf"{wake_prefix}{wake_core}\s*[,.\s]*", s, re.IGNORECASE)
    if m:
        rest = s[m.end() :].strip().lstrip(",.; ")
        if not rest:
            return "", "ack"
        return rest, "ok"
    m2 = re.match(rf"^\s*{wake_core}\s*[,.\s]*\s*(.*)$", s, re.IGNORECASE | re.DOTALL)
    if m2:
        rest = (m2.group(1) or "").strip()
        if not rest:
            return "", "ack"
        return rest, "ok"
    m3 = re.search(rf"(?:^|[\s,;])({wake_core})(?=[\s,;]|$)", s, re.IGNORECASE)
    if m3:
        rest = s[m3.end() :].strip().lstrip(",.; ")
        if not rest:
            return "", "ack"
        return rest, "ok"
    return None, "miss"
