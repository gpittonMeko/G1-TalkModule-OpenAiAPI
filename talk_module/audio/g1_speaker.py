"""
Riproduzione sulla cassa interna del robot G1 via unitree_sdk2py AudioClient.PlayStream.
Richiede PCM 16 kHz, mono, 16-bit little-endian (come l'esempio ufficiale SDK).
"""

from __future__ import annotations

import subprocess
import tempfile
import time
from pathlib import Path
from typing import Optional

from talk_module.stt.audio_convert import _ffmpeg_candidates

_APP_NAME = "talk_module"
_CHUNK_SIZE = 96000  # 3 s @ 16 kHz mono s16le
_BYTES_PER_SEC = 32000  # 16000 Hz * 2 byte


def _wav_to_pcm16_mono_16k(wav_bytes: bytes) -> Optional[bytes]:
    """Converte WAV/MP3 in raw PCM s16le 16 kHz mono (tolerante header WAV corrotti)."""
    if not wav_bytes or len(wav_bytes) < 44:
        return None
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as fin:
        fin.write(wav_bytes)
        inp = Path(fin.name)
    out = inp.with_suffix(".pcm")
    try:
        for ff in _ffmpeg_candidates():
            if not ff:
                continue
            try:
                r = subprocess.run(
                    [
                        ff,
                        "-y",
                        "-fflags",
                        "+discardcorrupt",
                        "-err_detect",
                        "ignore_err",
                        "-i",
                        str(inp),
                        "-acodec",
                        "pcm_s16le",
                        "-ar",
                        "16000",
                        "-ac",
                        "1",
                        "-af",
                        "aresample=16000",
                        "-f",
                        "s16le",
                        str(out),
                    ],
                    capture_output=True,
                    timeout=120,
                )
                if r.returncode == 0 and out.exists() and out.stat().st_size > 0:
                    pcm = out.read_bytes()
                    # Normalizza: evita click/EEE se i primi campioni sono spike da header corrotto.
                    return _trim_leading_spikes(pcm)
            except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
                continue
        return None
    finally:
        inp.unlink(missing_ok=True)
        out.unlink(missing_ok=True)


def _trim_leading_spikes(pcm: bytes, max_ms: int = 80, threshold: int = 12000) -> bytes:
    """Salta l'inizio se campioni PCM anomali (tipico di WAV con RIFF size errato)."""
    if len(pcm) < 640:
        return pcm
    import struct

    max_samples = min(len(pcm) // 2, int(16000 * max_ms / 1000))
    start = 0
    for i in range(max_samples):
        sample = struct.unpack_from("<h", pcm, i * 2)[0]
        if abs(sample) < threshold:
            start = i * 2
            break
    else:
        start = 0
    if start > 0:
        pcm = pcm[start:]
    return pcm


def _init_audio_client():
    """AudioClient G1 (chiamare solo con _sdk_lock acquisito)."""
    from talk_module.robot_actions import _ensure_dds_init
    from unitree_sdk2py.g1.audio.g1_audio_client import AudioClient

    _ensure_dds_init()
    client = AudioClient()
    client.SetTimeout(10.0)
    client.Init()
    return client


def play_pcm_on_g1(pcm: bytes) -> bool:
    """Invia PCM alla cassa interna G1 a chunk via DDS AudioClient."""
    if not pcm or len(pcm) < 640:
        return False
    try:
        from talk_module.robot_actions import _sdk_lock
    except ImportError:
        return False

    duration_sec = len(pcm) / _BYTES_PER_SEC
    try:
        with _sdk_lock:
            client = _init_audio_client()
            # Chiudi eventuale stream precedente.
            try:
                client.PlayStop(_APP_NAME)
            except Exception:
                pass
            time.sleep(0.05)

            stream_id = str(int(time.time() * 1000))
            offset = 0
            total = len(pcm)
            t0 = time.time()

            while offset < total:
                chunk = pcm[offset : offset + _CHUNK_SIZE]
                code, _ = client.PlayStream(_APP_NAME, stream_id, chunk)
                if code != 0:
                    print(f"[g1_speaker] PlayStream rc={code} offset={offset}", flush=True)
                    return False
                offset += len(chunk)
                # Pacing ~real-time (come esempio SDK: non saturare il buffer).
                if offset < total:
                    time.sleep(max(0.5, len(chunk) / _BYTES_PER_SEC))

            # Attendi fine riproduzione PRIMA di PlayStop (altrimenti taglia con "EEE"/click).
            elapsed = time.time() - t0
            tail = duration_sec - elapsed + 0.35
            if tail > 0:
                time.sleep(tail)
            try:
                client.PlayStop(_APP_NAME)
            except Exception:
                pass
        print(
            f"[g1_speaker] ok pcm={len(pcm)} dur={duration_sec:.2f}s elapsed={elapsed:.2f}s",
            flush=True,
        )
        return True
    except ImportError:
        return False
    except Exception as e:
        print(f"[g1_speaker] errore: {e}", flush=True)
        return False


def play_wav_on_g1(wav_bytes: bytes) -> bool:
    """Converte WAV e riproduce sulla cassa interna del G1."""
    pcm = _wav_to_pcm16_mono_16k(wav_bytes)
    if not pcm:
        print("[g1_speaker] conversione PCM fallita", flush=True)
        return False
    return play_pcm_on_g1(pcm)
