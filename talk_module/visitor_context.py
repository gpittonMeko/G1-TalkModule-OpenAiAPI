"""Profilo visitatori/ospiti per contesto LLM e saluti (config/visitor_profiles.json)."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Optional

from talk_module.config import settings


def _visitor_profile_from_env() -> str:
    return (getattr(settings, "visitor_profile", None) or os.getenv("G1_VISITOR_PROFILE", "") or "").strip().lower()

_PROFILES_PATH = Path(__file__).resolve().parent.parent / "config" / "visitor_profiles.json"
_cache: Optional[dict[str, Any]] = None


def _load_profiles() -> dict[str, Any]:
    global _cache
    if _cache is not None:
        return _cache
    if not _PROFILES_PATH.exists():
        _cache = {}
        return _cache
    try:
        raw = json.loads(_PROFILES_PATH.read_text(encoding="utf-8"))
        _cache = {k: v for k, v in (raw or {}).items() if not str(k).startswith("_") and isinstance(v, dict)}
    except Exception:
        _cache = {}
    return _cache


def reload_visitor_profiles() -> None:
    global _cache
    _cache = None


def active_visitor_profile_id() -> str:
    return _visitor_profile_from_env()


def get_active_visitor_profile() -> Optional[dict[str, Any]]:
    pid = active_visitor_profile_id()
    if not pid:
        return None
    return _load_profiles().get(pid)


def get_visitor_system_supplement() -> str:
    prof = get_active_visitor_profile()
    if not prof:
        return ""
    return str(prof.get("system_supplement") or "").strip()


def get_visitor_hey_ack_override() -> Optional[str]:
    prof = get_active_visitor_profile()
    if not prof:
        return None
    ack = str(prof.get("hey_g1_ack") or "").strip()
    return ack or None


def get_hey_g1_ack_response() -> str:
    """Risposta TTS per solo wake word (Hey G1 senza comando)."""
    ack = get_visitor_hey_ack_override()
    if ack:
        return ack
    return (settings.hey_g1_ack_text or "").strip() or "Sì, ti ascolto. Come posso aiutarti?"


def get_visitor_whisper_hint() -> str:
    prof = get_active_visitor_profile()
    if not prof:
        return ""
    return str(prof.get("whisper_hint") or "").strip()


def check_visitor_greeting(user_input: str) -> Optional[str]:
    """
    Saluto scriptato per visita attiva (es. buongiorno → messaggio Meko/Alpitronic).
    Evita match su domande lunghe che contengono solo la parola «ciao».
    """
    prof = get_active_visitor_profile()
    if not prof:
        return None
    response = str(prof.get("greeting_response") or "").strip()
    if not response:
        return None
    txt = (user_input or "").strip().lower()
    if not txt:
        return None
    triggers = prof.get("greeting_triggers") or []
    if not isinstance(triggers, list):
        return None
    for pattern in sorted((str(p).strip().lower() for p in triggers if p), key=len, reverse=True):
        if not pattern or pattern not in txt:
            continue
        words = txt.split()
        if len(words) <= 8:
            return response
        if txt.startswith(pattern) and len(words) <= 12:
            return response
    return None


def list_visitor_profiles() -> dict[str, str]:
    return {k: str(v.get("label") or k) for k, v in _load_profiles().items()}
