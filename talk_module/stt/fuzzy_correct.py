"""
Correzione fuzzy STT: sostituisce parole trascritte erroneamente con le più vicine
nel vocabolario (knowledge.json + extra_phrases). Usa difflib (stdlib, no deps).
"""

import json
import re
from difflib import SequenceMatcher
from pathlib import Path
from typing import Optional

# Paths
_root = Path(__file__).resolve().parent.parent.parent
KNOWLEDGE_PATH = _root / "config" / "knowledge.json"
STT_CONFIG_PATH = _root / "config" / "stt_config.json"
ITALIAN_VOCAB_PATH = _root / "config" / "italian_vocabulary.txt"


def _load_italian_vocabulary(path: Optional[Path] = None) -> set[str]:
    """Carica vocabolario italiano da file (una parola per riga)."""
    p = path or ITALIAN_VOCAB_PATH
    if not p.exists():
        return set()
    words: set[str] = set()
    for line in p.read_text(encoding="utf-8").splitlines():
        w = line.strip().split("#")[0].strip().lower()
        if w and len(w) >= 2:
            words.add(w)
    return words


def _extract_words(phrase: str) -> set[str]:
    """Estrae parole da una frase (solo lettere, min 2 caratteri)."""
    words = re.findall(r"[a-zA-ZàèéìòùçÀÈÉÌÒÙÇ]+", phrase.lower())
    return {w for w in words if len(w) >= 2}


def get_vocabulary(
    knowledge: dict[str, str],
    extra_phrases: Optional[list[str]] = None,
) -> tuple[set[str], list[str]]:
    """
    Costruisce vocabolario da knowledge.json + extra_phrases.
    Ritorna (parole_uniche, frasi_intere).
    """
    words: set[str] = set()
    phrases: list[str] = []
    for pattern in knowledge.keys():
        if pattern and pattern.strip():
            p = pattern.strip().lower()
            phrases.append(p)
            words.update(_extract_words(p))
    for phrase in extra_phrases or []:
        if phrase and phrase.strip():
            p = phrase.strip().lower()
            phrases.append(p)
            words.update(_extract_words(p))
    return words, phrases


def _load_stt_config() -> dict:
    """Carica config/stt_config.json."""
    if not STT_CONFIG_PATH.exists():
        return {}
    try:
        return json.loads(STT_CONFIG_PATH.read_text(encoding="utf-8")) or {}
    except Exception:
        return {}


def _get_vocabulary_and_params(knowledge: dict[str, str]) -> tuple[set[str], list[str], float, int]:
    """Carica vocabolario e parametri. Ritorna (words, phrases, threshold, min_word_length)."""
    from talk_module.config import settings
    cfg = _load_stt_config()
    extra = cfg.get("extra_phrases") or []
    threshold = settings.stt_fuzzy_threshold
    min_len = settings.stt_min_word_length
    words, phrases = get_vocabulary(knowledge, extra)
    # Focalizza su vocabolario italiano: merge con parole comuni da italian_vocabulary.txt
    if cfg.get("use_italian_vocabulary", True):
        custom_path = cfg.get("italian_vocabulary_path")
        if custom_path:
            words.update(_load_italian_vocabulary(Path(custom_path)))
        else:
            words.update(_load_italian_vocabulary())
    return words, phrases, threshold, min_len


def _similarity(a: str, b: str) -> float:
    """Similarità 0-1 tra due stringhe (SequenceMatcher.ratio)."""
    if not a or not b:
        return 0.0
    return SequenceMatcher(None, a.lower(), b.lower()).ratio()


def correct_transcript(
    text: str,
    vocabulary_words: set[str],
    vocabulary_phrases: list[str],
    threshold: float = 0.85,
    min_word_length: int = 3,
) -> str:
    """
    Corregge trascrizione STT con fuzzy matching.
    - Prima: match frase intera (se transcript molto simile a frase nota)
    - Poi: per ogni parola non nel vocabolario, sostituisci se c'è match >= threshold
    """
    if not text or not text.strip():
        return text
    txt = text.strip()
    txt_lower = txt.lower()

    # 1. Match frase intera: se transcript è molto simile a una frase nota, usa quella
    for phrase in sorted(vocabulary_phrases, key=len, reverse=True):
        if len(phrase) < 5:
            continue
        sim = _similarity(txt_lower, phrase)
        if sim >= 0.95:
            return phrase

    # 2. Match a livello parola (token: parole o sequenze non-parola)
    words_in_text = re.findall(r"[a-zA-ZàèéìòùçÀÈÉÌÒÙÇ]+|[^a-zA-ZàèéìòùçÀÈÉÌÒÙÇ]+", txt)
    result_parts: list[str] = []
    word_pattern = re.compile(r"^[a-zA-ZàèéìòùçÀÈÉÌÒÙÇ]+$")
    for token in words_in_text:
        if not word_pattern.match(token):
            result_parts.append(token)
            continue
        word = token.lower()
        if len(word) < min_word_length:
            result_parts.append(token)
            continue
        if word in vocabulary_words:
            result_parts.append(token)
            continue
        best_match: Optional[str] = None
        best_ratio = 0.0
        for v in vocabulary_words:
            if len(v) < min_word_length:
                continue
            r = _similarity(word, v)
            if r > best_ratio and r >= threshold:
                best_ratio = r
                best_match = v
        if best_match is not None:
            result_parts.append(best_match)
        else:
            result_parts.append(token)
    return "".join(result_parts)


def apply_fuzzy_correction(text: str, knowledge: dict[str, str]) -> str:
    """
    Entry point: carica vocabolario (knowledge + stt_config) e applica correzione.
    Usato da web_app dopo stt.transcribe().
    """
    if not text or not text.strip():
        return text
    words, phrases, threshold, min_len = _get_vocabulary_and_params(knowledge)
    if not words and not phrases:
        return text
    return correct_transcript(text, words, phrases, threshold, min_len)
