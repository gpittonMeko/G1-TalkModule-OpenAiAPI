"""Knowledge base: grouped pattern -> response with per-group enable toggle."""

from __future__ import annotations

import json
import os
import re
import uuid
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

_DEFAULT_GROUP_ID = "general"

_STOP_WORDS = frozenset({
    "che", "chi", "di", "da", "per", "con", "su", "tra", "fra", "del", "della", "dei", "delle",
    "il", "lo", "la", "le", "li", "gli", "un", "una", "uno", "al", "nel", "dal", "sul", "col",
    "ma", "se", "non", "più", "già", "mai", "poi", "io", "tu", "lui", "lei", "noi", "voi",
    "ora", "ore", "era", "sono", "sei", "hai", "può", "sì", "no", "ok", "va", "fa", "sa",
    "cosa", "come", "dove", "quando", "molto", "poco", "bene", "male", "questo", "quello",
    "quella", "questi", "mi", "ti", "ci", "vi", "lo", "ne", "e", "o", "a", "in", "è", "sono",
    "puoi", "può", "vorrei", "voglio", "dimmi", "raccontami", "parlami", "spiegami", "dici",
    "dire", "sai", "sapere", "qualcosa", "qualcuno", "qualche", "sulla", "sullo", "sulle",
    "sui", "degli", "delle", "degli", "the",
})

_groups_cache: list[dict[str, Any]] | None = None
_active_cache: dict[str, str] | None = None


def knowledge_path() -> Path:
    return Path(__file__).resolve().parent.parent / "config" / "knowledge.json"


def _slug_id(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", (name or "").strip().lower()).strip("_")
    return slug or str(uuid.uuid4())[:8]


def _clean_entries(raw: Any) -> dict[str, str]:
    if not isinstance(raw, dict):
        return {}
    out: dict[str, str] = {}
    for key, value in raw.items():
        if key and value is not None and str(key).strip():
            out[str(key).strip()] = str(value).strip()
    return out


def _normalize_group(raw: dict[str, Any], fallback_id: str = "") -> dict[str, Any]:
    group_id = str(raw.get("id") or fallback_id or _slug_id(str(raw.get("name") or "group"))).strip()
    name = str(raw.get("name") or group_id).strip() or group_id
    enabled = bool(raw.get("enabled", True))
    entries = _clean_entries(raw.get("entries") or raw.get("patterns") or {})
    return {"id": group_id, "name": name, "enabled": enabled, "entries": entries}


def _auto_split_groups(entries: dict[str, str]) -> list[dict[str, Any]]:
    rules = [
        ("mckinsey", "McKinsey", ("mckinsey", "kinsey", "centenario", "cent'anni", "31 marzo")),
        ("alpitronic", "Alpitronic", ("alpitronic", "hypercharger")),
        ("granzotto", "Granzotto", ("granzotto",)),
        ("general", "Generale", ()),
    ]
    buckets: dict[str, dict[str, str]] = {group_id: {} for group_id, _, _ in rules}
    for pattern, response in entries.items():
        pattern_lower = pattern.lower()
        matched = False
        for group_id, _, keywords in rules[:-1]:
            if any(keyword in pattern_lower for keyword in keywords):
                buckets[group_id][pattern] = response
                matched = True
                break
        if not matched:
            buckets["general"][pattern] = response
    groups = [
        {"id": group_id, "name": name, "enabled": True, "entries": buckets[group_id]}
        for group_id, name, _ in rules
        if buckets[group_id]
    ]
    if groups:
        return groups
    return [{"id": _DEFAULT_GROUP_ID, "name": "Generale", "enabled": True, "entries": entries}]


def _parse_flat_as_groups(data: dict[str, Any]) -> list[dict[str, Any]]:
    entries = _clean_entries(data)
    if not entries:
        return [{"id": _DEFAULT_GROUP_ID, "name": "Generale", "enabled": True, "entries": {}}]
    return _auto_split_groups(entries)


def load_knowledge_groups() -> list[dict[str, Any]]:
    global _groups_cache
    if _groups_cache is not None:
        return _groups_cache

    path = knowledge_path()
    if not path.exists():
        _groups_cache = [{"id": _DEFAULT_GROUP_ID, "name": "Generale", "enabled": True, "entries": {}}]
        return _groups_cache

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        _groups_cache = [{"id": _DEFAULT_GROUP_ID, "name": "Generale", "enabled": True, "entries": {}}]
        return _groups_cache

    if isinstance(data, dict) and "groups" in data:
        groups_raw = data.get("groups") or []
        _groups_cache = [
            _normalize_group(group, f"group_{index}")
            for index, group in enumerate(groups_raw)
            if isinstance(group, dict)
        ]
    elif isinstance(data, dict):
        _groups_cache = _parse_flat_as_groups(data)
    else:
        _groups_cache = [{"id": _DEFAULT_GROUP_ID, "name": "Generale", "enabled": True, "entries": {}}]

    if not _groups_cache:
        _groups_cache = [{"id": _DEFAULT_GROUP_ID, "name": "Generale", "enabled": True, "entries": {}}]
    return _groups_cache


def load_knowledge() -> dict[str, str]:
    """Active patterns only (enabled groups), keys lowercased for matching."""
    global _active_cache
    if _active_cache is not None:
        return _active_cache

    merged: dict[str, str] = {}
    for group in load_knowledge_groups():
        if not group.get("enabled", True):
            continue
        for pattern, response in (group.get("entries") or {}).items():
            key = str(pattern).strip().lower()
            if key and response:
                merged[key] = str(response).strip()
    _active_cache = merged
    return _active_cache


def reload_knowledge() -> None:
    global _groups_cache, _active_cache
    _groups_cache = None
    _active_cache = None


def save_knowledge_groups(groups: list[dict[str, Any]]) -> list[dict[str, Any]]:
    clean: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for index, group in enumerate(groups or []):
        if not isinstance(group, dict):
            continue
        normalized = _normalize_group(group, f"group_{index}")
        base_id = normalized["id"]
        group_id = base_id
        suffix = 2
        while group_id in seen_ids:
            group_id = f"{base_id}_{suffix}"
            suffix += 1
        normalized["id"] = group_id
        seen_ids.add(group_id)
        clean.append(normalized)

    if not clean:
        clean = [{"id": _DEFAULT_GROUP_ID, "name": "Generale", "enabled": True, "entries": {}}]

    knowledge_path().write_text(
        json.dumps({"groups": clean}, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    reload_knowledge()
    return clean


def save_knowledge_entries(entries: dict[str, str]) -> list[dict[str, Any]]:
    """Legacy flat save: store everything in one enabled group."""
    return save_knowledge_groups(
        [{"id": _DEFAULT_GROUP_ID, "name": "Generale", "enabled": True, "entries": _clean_entries(entries)}]
    )


def _knowledge_fuzzy_enabled() -> bool:
    return os.getenv("G1_KNOWLEDGE_FUZZY", "1").strip().lower() not in ("0", "false", "no", "off")


def _knowledge_fuzzy_threshold() -> float:
    try:
        return float(os.getenv("G1_KNOWLEDGE_FUZZY_THRESHOLD", "0.82"))
    except ValueError:
        return 0.82


def _normalize_match_text(text: str) -> str:
    text = (text or "").lower().strip()
    text = re.sub(r"[^\w\s']", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _fuzzy_contains(text: str, pattern: str, threshold: float = 0.82) -> bool:
    """True if text contains pattern exactly or a very similar substring."""
    if pattern in text:
        return True
    if len(pattern.split()) < 2:
        return False
    pattern_len = len(pattern)
    margin = max(3, pattern_len // 4)
    for start in range(0, max(1, len(text) - pattern_len + margin + 1)):
        end = min(len(text), start + pattern_len + margin)
        window = text[start:end]
        if SequenceMatcher(None, window, pattern).ratio() >= threshold:
            return True
    return False


def _significant_tokens(pattern: str) -> list[str]:
    return [
        word
        for word in _normalize_match_text(pattern).split()
        if word not in _STOP_WORDS and len(word) >= 2
    ]


def _token_matches(token: str, text: str, threshold: float = 0.85) -> bool:
    if token in text:
        return True
    words = re.findall(r"[a-zA-Zàèéìòùç']+", text)
    return any(SequenceMatcher(None, token, word).ratio() >= threshold for word in words)


def _pattern_matches(text: str, pattern: str, *, fuzzy: bool, threshold: float) -> bool:
    normalized_text = _normalize_match_text(text)
    normalized_pattern = _normalize_match_text(pattern)
    if not normalized_text or not normalized_pattern:
        return False
    if normalized_pattern in normalized_text:
        return True
    if not fuzzy:
        return False
    if _fuzzy_contains(normalized_text, normalized_pattern, threshold=threshold):
        return True

    tokens = _significant_tokens(normalized_pattern)
    if not tokens:
        return False
    if len(tokens) == 1:
        return _token_matches(tokens[0], normalized_text, threshold=threshold)
    matched = sum(1 for token in tokens if _token_matches(token, normalized_text, threshold=threshold))
    return matched == len(tokens)


def check_knowledge_voice(user_input: str) -> str | None:
    """Voice path: raw STT first, then word-level STT fuzzy retry (no phrase replacement)."""
    resp = check_knowledge(user_input)
    if resp:
        return resp
    try:
        from talk_module.stt.fuzzy_correct import apply_fuzzy_correction

        corrected = apply_fuzzy_correction(user_input, load_knowledge())
    except Exception:
        return None
    if not corrected or corrected.strip().lower() == user_input.strip().lower():
        return None
    print(f"[knowledge] voice retry after STT fuzzy: {user_input!r} -> {corrected!r}", flush=True)
    return check_knowledge(corrected)


def check_knowledge(user_input: str) -> str | None:
    if not user_input or not user_input.strip():
        return None
    try:
        from talk_module.visitor_context import check_visitor_greeting

        visitor_greeting = check_visitor_greeting(user_input)
        if visitor_greeting:
            return visitor_greeting
    except Exception:
        pass

    text = user_input.strip().lower()
    fuzzy = _knowledge_fuzzy_enabled()
    threshold = _knowledge_fuzzy_threshold()
    patterns = sorted(load_knowledge().items(), key=lambda item: -len(item[0]))
    for pattern, response in patterns:
        if pattern and _pattern_matches(text, pattern, fuzzy=False, threshold=threshold):
            print(f"[knowledge] exact match pattern={pattern!r} input={user_input!r}", flush=True)
            return response
    if fuzzy:
        for pattern, response in patterns:
            if pattern and _pattern_matches(text, pattern, fuzzy=True, threshold=threshold):
                print(f"[knowledge] fuzzy match pattern={pattern!r} input={user_input!r}", flush=True)
                return response
    return None
