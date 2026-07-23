"""Decodifica audio soundboard (mp3/webm/…) in WAV e PCM per cassa G1."""

from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path

from talk_module.audio.g1_speaker import _wav_to_pcm16_mono_16k
from talk_module.stt.audio_convert import _ffmpeg_candidates


def _ffmpeg_exe() -> str | None:
    for ff in _ffmpeg_candidates():
        if ff:
            return ff
    return None


def soundboard_bytes_to_wav(raw: bytes, fmt: str) -> bytes:
    """Converte slot audio in WAV PCM (44.1 kHz stereo) per playback Jetson."""
    if len(raw) >= 12 and raw[:4] == b"RIFF" and raw[8:12] == b"WAVE":
        return raw
    fmt_l = (fmt or "mp3").lower().split(";")[0].strip()
    if "webm" in fmt_l:
        suf = ".webm"
    elif "wav" in fmt_l:
        return raw
    elif "mp3" in fmt_l or fmt_l == "mpeg" or fmt_l == "audio/mpeg":
        suf = ".mp3"
    else:
        suf = ".webm"
    ff = _ffmpeg_exe()
    if not ff:
        raise RuntimeError("ffmpeg non trovato")
    with tempfile.NamedTemporaryFile(suffix=suf, delete=False) as f:
        f.write(raw)
        inp = Path(f.name)
    out = inp.with_suffix(".wav")
    try:
        r = subprocess.run(
            [
                ff,
                "-y",
                "-i",
                str(inp),
                "-acodec",
                "pcm_s16le",
                "-ar",
                "44100",
                "-ac",
                "2",
                str(out),
            ],
            capture_output=True,
            timeout=120,
        )
        if r.returncode != 0 or not out.exists():
            err = (r.stderr or b"").decode("utf-8", errors="replace")[-400:]
            raise RuntimeError(err or "ffmpeg decode failed")
        return out.read_bytes()
    finally:
        inp.unlink(missing_ok=True)
        out.unlink(missing_ok=True)


def soundboard_bytes_to_pcm_g1(raw: bytes, fmt: str) -> bytes | None:
    """Raw slot → PCM 16 kHz mono per AudioClient G1."""
    wav = soundboard_bytes_to_wav(raw, fmt)
    return _wav_to_pcm16_mono_16k(wav)
