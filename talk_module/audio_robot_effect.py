"""Effetto vocale robotico su audio (base64 in/out) — ring modulator, bitcrusher, bandpass."""

from __future__ import annotations

import base64
import math
from io import BytesIO
from typing import Literal

EffectPreset = Literal["telephone", "ring_mod", "bitcrush", "robot_full"]


def _get_samples(seg) -> tuple[list[int], int]:
    """Estrae campioni mono da pydub AudioSegment. Ritorna (samples, sample_rate)."""
    import numpy as np

    if seg.channels == 2:
        seg = seg.split_to_mono()[0]
    samples = np.array(seg.get_array_of_samples(), dtype=np.float32) / 32768.0
    return samples, seg.frame_rate


def _samples_to_segment(samples, sample_rate: int, channels: int = 1):
    """Converte array numpy in pydub AudioSegment."""
    from pydub import AudioSegment
    import numpy as np

    samples = np.clip(samples, -1.0, 1.0)
    raw = (samples * 32767).astype(np.int16)
    return AudioSegment(
        raw.tobytes(),
        frame_rate=sample_rate,
        sample_width=2,
        channels=channels,
    )


def _apply_ring_mod(samples, sample_rate: int, carrier_hz: float = 95, mix: float = 0.6) -> list:
    """Ring modulator: moltiplica per onda portante → suono metallico/robotico."""
    import numpy as np

    n = len(samples)
    t = np.arange(n, dtype=np.float32) / sample_rate
    carrier = np.sin(2 * math.pi * carrier_hz * t).astype(np.float32)
    modulated = samples * carrier
    return (1 - mix) * samples + mix * modulated


def _apply_bitcrusher(samples, bit_depth: int = 6, mix: float = 0.7) -> list:
    """Bitcrusher: riduce profondità bit → distorsione digitale lo-fi."""
    import numpy as np

    levels = 2**bit_depth
    quantized = np.round(samples * (levels - 1)) / (levels - 1)
    return (1 - mix) * samples + mix * quantized


def _apply_bandpass(seg, low_hz: int = 300, high_hz: int = 2200):
    """Bandpass stile telefono."""
    return seg.high_pass_filter(low_hz).low_pass_filter(high_hz)


def apply_robot_effect_base64(
    audio_b64: str,
    fmt: str = "webm",
    preset: EffectPreset = "robot_full",
) -> tuple[str, str]:
    """
    Applica effetto robotico su audio base64.
    Preset:
      - telephone: solo bandpass (vecchio stile)
      - ring_mod: ring modulator metallico
      - bitcrush: bitcrusher lo-fi
      - robot_full: ring mod + bitcrush + bandpass (effetto robot marcato)
    Ritorna (base64, formato).
    """
    if not audio_b64 or len(audio_b64) < 100:
        return audio_b64, fmt
    try:
        from pydub import AudioSegment
        import numpy as np

        raw = base64.b64decode(audio_b64)
        f = (fmt or "webm").lower()
        if f == "wav":
            seg = AudioSegment.from_wav(BytesIO(raw))
        else:
            seg = AudioSegment.from_file(BytesIO(raw), format=f)

        if preset != "telephone":
            samples, sr = _get_samples(seg)
            if preset == "ring_mod":
                samples = _apply_ring_mod(samples, sr, carrier_hz=95, mix=0.55)
            elif preset == "bitcrush":
                samples = _apply_bitcrusher(samples, bit_depth=6, mix=0.65)
            elif preset == "robot_full":
                samples = _apply_ring_mod(samples, sr, carrier_hz=90, mix=0.5)
                samples = _apply_bitcrusher(samples, bit_depth=7, mix=0.5)
            seg = _samples_to_segment(samples, sr)

        seg = _apply_bandpass(seg, low_hz=280, high_hz=2400)

        out = BytesIO()
        try:
            seg.export(out, format="mp3", bitrate="128k")
            return base64.b64encode(out.getvalue()).decode(), "mp3"
        except Exception:
            out = BytesIO()
            seg.export(out, format="wav")
            return base64.b64encode(out.getvalue()).decode(), "wav"
    except Exception:
        return audio_b64, fmt
