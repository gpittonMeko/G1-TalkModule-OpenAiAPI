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
    "you",
    # Spagnolo (allucinazioni Whisper comuni)
    "hola",
    "buenos días",
    "buenos dias",
    "buenas tardes",
    "buenas noches",
    "gracias",
    "muchas gracias",
    "por favor",
    "qué tal",
    "que tal",
    "cómo estás",
    "como estas",
    "adiós",
    "adios",
    "señor",
    "senor",
    "señora",
    "senora",
    "subtítulos",
    "subtitulos",
    "español",
    "espanol",
    "sí,",
    "si,",
)

from talk_module.stt_prompts import ITALIAN_WAKE_WHISPER_PROMPT

WAKE_STT_PROMPT = ITALIAN_WAKE_WHISPER_PROMPT

# STT confonde spesso "G1" / "G one" con nomi italiani (Bepi) o sillabe simili.
_G1_MISHEAR_CORE = (
    r"b[eéè]pp?[iy]'?"  # beppy, bepp (prima di bepi: evita match parziale "bep")
    r"|b[eéè][\s\-]*p[iíì']?"  # be pi, be-pi
    r"|b[eéè]p[iíì']?"  # bepi, bepì
    r"|b[eéè]pee"
    r"|j[eéè]{1,2}epy"  # jeepy, jepy
    r"|pep[iíì']?"
    r"|gep[iíì']?"
    r"|g[iíì]p[iíì']?"
    r"|j[eéè]p[iíì']?"
    r"|j[iíì]p[iíì']?"
    r"|bebb?y"  # beby, bebby
    r"|b[eéè]bi"
)

_WAKE_PREFIX_RE = r"(?:hey|ehi|ei|e|sei|hai)\s*[,.\s]*"
_WAKE_TOKEN_BOUNDARY = r"(?=[\s,;.'\"!?]|$)"


def normalize_wake_stt_text(text: str) -> str:
    """
    Corregge trascritti STT che scrivono Bepi/Bepì/be pi al posto di G1.
    Applica solo all'inizio frase (dopo prefisso wake oppure da sola).
    """
    if not text or not text.strip():
        return text
    s = text.strip()
    with_prefix = re.sub(
        rf"(?i)^({_WAKE_PREFIX_RE})({_G1_MISHEAR_CORE}){_WAKE_TOKEN_BOUNDARY}",
        r"\1g1",
        s,
        count=1,
    )
    if with_prefix != s:
        return with_prefix
    return re.sub(
        rf"(?i)^({_G1_MISHEAR_CORE}){_WAKE_TOKEN_BOUNDARY}",
        "g1",
        s,
        count=1,
    )


def wake_display_text(raw_text: str, wkind: str) -> str:
    """Testo mostrato in UI/log dopo wake: sempre Hey G1 se solo ack."""
    if wkind == "ack":
        return "Hey G1"
    if wkind == "ok":
        return normalize_wake_stt_text(raw_text)
    return raw_text


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
    Ascolto continuo: rileva wake (Hey G1, G1, G one, gi one, Mark one, Bepi→G1, …).
    Ritorna (resto dopo wake, kind) con kind: miss | ack | ok.
    rest None se miss; stringa vuota se solo wake; altrimenti testo comando.
    Gestisce varianti Whisper: giuno, gi uno, di uno, gì uno, bepi, bepì, be pi, etc.
    """
    if not t or not t.strip():
        return None, "miss"
    s = normalize_wake_stt_text(t.strip())
    if is_stt_hallucination(s):
        return None, "miss"
    # Varianti STT: g1, g one, gi one, giuno, di uno, gì uno, jee one, ji one, mark one, j1, g-1, bepi…
    wake_core = (
        r"(?:"
        r"g[\s\-]*1"  # g1, g 1, g-1
        r"|\bg1\b"
        r"|g[\s\-]*one"  # g one
        r"|g[\s\-]*uno"  # g uno
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
        rf"|{_G1_MISHEAR_CORE}"  # bepi, bepì, be pi, pepi, gepi, …
        r")"
    )
    # Prefissi wake: hey, ehi, ei, e, sei, hai (Whisper trascrive "hai" per "hey", "sei di uno" per "ehi G1")
    wake_prefix = rf"(?:{_WAKE_PREFIX_RE})"
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
