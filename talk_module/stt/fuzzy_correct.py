"""
Correzione fuzzy STT: sostituisce parole trascritte erroneamente con le più vicine
nel vocabolario (knowledge.json + extra_phrases). Usa difflib (stdlib, no deps).
"""

import json
import re
from difflib import SequenceMatcher
from pathlib import Path
from typing import Optional

_root = Path(__file__).resolve().parent.parent.parent
KNOWLEDGE_PATH = _root / "config" / "knowledge.json"
STT_CONFIG_PATH = _root / "config" / "stt_config.json"
ITALIAN_VOCAB_PATH = _root / "config" / "italian_vocabulary.txt"

_PROTECTED_WORDS = frozenset({
    "che", "chi", "di", "da", "per", "con", "su", "tra", "fra",
    "il", "lo", "la", "le", "li", "gli", "un", "una", "uno",
    "al", "del", "nel", "dal", "sul", "col",
    "ma", "se", "non", "più", "già", "mai", "poi",
    "io", "tu", "lui", "lei", "noi", "voi",
    "ora", "ore", "era", "sono", "sei", "hai", "può",
    "sì", "no", "ok", "va", "fa", "sa",
    "cosa", "come", "dove", "quando",
    "molto", "poco", "bene", "male",
    "questo", "quello", "quella", "questi",
})


def _load_italian_vocabulary(path: Optional[Path] = None) -> set[str]:
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
    words = re.findall(r"[a-zA-ZàèéìòùçÀÈÉÌÒÙÇ]+", phrase.lower())
    return {w for w in words if len(w) >= 2}


def get_vocabulary(
    knowledge: dict[str, str],
    extra_phrases: Optional[list[str]] = None,
) -> tuple[set[str], list[str]]:
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
    if not STT_CONFIG_PATH.exists():
        return {}
    try:
        return json.loads(STT_CONFIG_PATH.read_text(encoding="utf-8")) or {}
    except Exception:
        return {}


def _get_vocabulary_and_params(knowledge: dict[str, str]) -> tuple[set[str], list[str], float, int]:
    from talk_module.config import settings
    cfg = _load_stt_config()
    extra = cfg.get("extra_phrases") or []
    cfg_threshold = cfg.get("threshold")
    cfg_min_len = cfg.get("min_word_length")
    threshold = float(cfg_threshold) if cfg_threshold is not None else settings.stt_fuzzy_threshold
    min_len = int(cfg_min_len) if cfg_min_len is not None else settings.stt_min_word_length
    words, phrases = get_vocabulary(knowledge, extra)
    if cfg.get("use_italian_vocabulary", True):
        custom_path = cfg.get("italian_vocabulary_path")
        if custom_path:
            words.update(_load_italian_vocabulary(Path(custom_path)))
        else:
            words.update(_load_italian_vocabulary())
    return words, phrases, threshold, min_len


def _similarity(a: str, b: str) -> float:
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
    if not text or not text.strip():
        return text
    txt = text.strip()

    words_in_text = re.findall(r"[a-zA-ZàèéìòùçÀÈÉÌÒÙÇ]+|[^a-zA-ZàèéìòùçÀÈÉÌÒÙÇ]+", txt)
    result_parts: list[str] = []
    word_pattern = re.compile(r"^[a-zA-ZàèéìòùçÀÈÉÌÒÙÇ]+$")
    corrections: list[str] = []
    for token in words_in_text:
        if not word_pattern.match(token):
            result_parts.append(token)
            continue
        word = token.lower()
        if len(word) < min_word_length:
            result_parts.append(token)
            continue
        if word in vocabulary_words or word in _PROTECTED_WORDS:
            result_parts.append(token)
            continue
        best_match: Optional[str] = None
        best_ratio = 0.0
        for v in vocabulary_words:
            if len(v) < min_word_length:
                continue
            if abs(len(v) - len(word)) > max(2, len(word) // 3):
                continue
            r = _similarity(word, v)
            if r > best_ratio and r >= threshold:
                best_ratio = r
                best_match = v
        if best_match is not None:
            corrections.append(f"'{token}'->'{best_match}'({best_ratio:.2f})")
            result_parts.append(best_match)
        else:
            result_parts.append(token)
    if corrections:
        print(f"[Fuzzy] Word corrections in '{txt}': {', '.join(corrections)}", flush=True)
    return "".join(result_parts)


def apply_fuzzy_correction(text: str, knowledge: dict[str, str]) -> str:
    if not text or not text.strip():
        return text
    words, phrases, threshold, min_len = _get_vocabulary_and_params(knowledge)
    if not words and not phrases:
        return text
    result = correct_transcript(text, words, phrases, threshold, min_len)
    if result != text:
        print(f"[Fuzzy] '{text}' -> '{result}'", flush=True)
    return result
