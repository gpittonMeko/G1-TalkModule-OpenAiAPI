"""Cache PCM G1 per slot soundboard — evita ffmpeg al Play e rispetta i ritardi."""

from __future__ import annotations

import hashlib
import threading

from talk_module.audio.soundboard_convert import soundboard_bytes_to_pcm_g1

_cache: dict[str, bytes] = {}
_pending: dict[str, threading.Event] = {}
_lock = threading.Lock()


def _cache_key(slot_idx: int, raw: bytes) -> str:
    digest = hashlib.sha256(raw).hexdigest()[:20]
    return f"{slot_idx}:{digest}"


def invalidate_slot_pcm(slot_idx: int) -> None:
    prefix = f"{int(slot_idx)}:"
    with _lock:
        for key in list(_cache):
            if key.startswith(prefix):
                _cache.pop(key, None)


def warmup_pcm(slot_idx: int, raw: bytes, fmt: str) -> None:
    """Avvia decode PCM in background (idempotente)."""
    if not raw or len(raw) < 80:
        return
    key = _cache_key(slot_idx, raw)
    with _lock:
        if key in _cache:
            return
        if key in _pending:
            return
        done = threading.Event()
        _pending[key] = done

    def _run() -> None:
        pcm: bytes | None = None
        try:
            pcm = soundboard_bytes_to_pcm_g1(raw, fmt)
        except Exception as e:
            print(f"[soundboard-pcm-cache] slot={slot_idx} decode error: {e}", flush=True)
        with _lock:
            if pcm:
                _cache[key] = pcm
            ev = _pending.pop(key, None)
        if ev:
            ev.set()

    threading.Thread(target=_run, daemon=True).start()


def get_pcm(slot_idx: int, raw: bytes, fmt: str, *, timeout: float = 120.0) -> bytes | None:
    """PCM pronto (attende decode se necessario)."""
    if not raw or len(raw) < 80:
        return None
    key = _cache_key(slot_idx, raw)
    with _lock:
        hit = _cache.get(key)
        if hit:
            return hit
        ev = _pending.get(key)
    if ev is None:
        warmup_pcm(slot_idx, raw, fmt)
        with _lock:
            ev = _pending.get(key)
    if ev:
        ev.wait(timeout=timeout)
    with _lock:
        return _cache.get(key)
