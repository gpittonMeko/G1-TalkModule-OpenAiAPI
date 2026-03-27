"""
Conversione audio dal browser (WebM, MP4/fMP4, OGG, MP3, WAV) in WAV 16k mono PCM
per API STT (OpenAI Whisper / Groq). La rilevazione del container dai magic bytes
ha priorità sul format_hint (MIME), così non si invia mai contenuto con estensione sbagliata.
"""

from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path
from typing import Optional, Tuple


def _ffmpeg_candidates() -> list[str]:
    """Ordine: bundle imageio-ffmpeg, poi PATH, poi percorsi tipici Linux (Jetson)."""
    seen: set[str] = set()
    out: list[str] = []
    try:
        import imageio_ffmpeg

        exe = imageio_ffmpeg.get_ffmpeg_exe()
        if exe and exe not in seen:
            seen.add(exe)
            out.append(exe)
    except Exception:
        pass
    for p in ("ffmpeg", "/usr/bin/ffmpeg", "/usr/local/bin/ffmpeg"):
        if p not in seen:
            seen.add(p)
            out.append(p)
    return out


def detect_container(audio_bytes: bytes) -> str:
    """Ritorna wav|webm|mp4|mp3|ogg|unknown."""
    if len(audio_bytes) < 12:
        return "unknown"
    if audio_bytes[:4] == b"RIFF" and audio_bytes[8:12] == b"WAVE":
        return "wav"
    if len(audio_bytes) >= 8 and audio_bytes[4:8] == b"ftyp":
        return "mp4"
    if len(audio_bytes) >= 4 and audio_bytes[:4] == b"OggS":
        return "ogg"
    if audio_bytes[:3] == b"ID3" or (
        len(audio_bytes) >= 2 and audio_bytes[:2] in (b"\xff\xfb", b"\xff\xf3", b"\xff\xf2")
    ):
        return "mp3"
    if (
        audio_bytes[0] == 0x1A
        and audio_bytes[1] == 0x45
        and audio_bytes[2] == 0xDF
        and audio_bytes[3] == 0xA3
    ):
        return "webm"
    return "unknown"


def coerce_format_hint(format_hint: Optional[str]) -> str:
    """Estrae un tipo grossolano da MIME o estensione (es. audio/webm;codecs=opus -> webm)."""
    if not format_hint:
        return ""
    h = format_hint.lower().strip()
    if "wav" in h:
        return "wav"
    if "audio/mp4" in h or "video/mp4" in h or "mp4" in h or "m4a" in h or "mpeg4" in h:
        return "mp4"
    if "webm" in h:
        return "webm"
    if "mp3" in h or h == "audio/mpeg":
        return "mp3"
    if "ogg" in h:
        return "ogg"
    return ""


def _is_valid_wav(b: bytes) -> bool:
    return len(b) > 100 and b[:4] == b"RIFF" and b[8:12] == b"WAVE"


def ffmpeg_bytes_to_wav(audio_bytes: bytes, input_suffix: str) -> Tuple[bytes, Optional[str]]:
    """
    Converte in WAV 16k mono PCM. input_suffix: webm, mp4, mp3, ogg, wav (senza punto).
    """
    input_suffix = (input_suffix or "webm").lstrip(".").lower()
    if input_suffix not in ("webm", "mp4", "m4a", "mp3", "ogg", "wav"):
        input_suffix = "webm"
    path_in = ""
    path_out = ""
    try:
        with tempfile.NamedTemporaryFile(suffix=f".{input_suffix}", delete=False) as f_in:
            f_in.write(audio_bytes)
            path_in = f_in.name
        path_out = str(Path(path_in).with_suffix(".wav"))
        last_err: Optional[str] = None
        for ff in _ffmpeg_candidates():
            if not ff:
                continue
            try:
                cp = subprocess.run(
                    [
                        ff,
                        "-y",
                        "-i",
                        path_in,
                        "-acodec",
                        "pcm_s16le",
                        "-ar",
                        "16000",
                        "-ac",
                        "1",
                        path_out,
                    ],
                    capture_output=True,
                    timeout=45,
                    check=False,
                )
                if cp.returncode != 0:
                    err_txt = (cp.stderr or b"").decode("utf-8", errors="replace")[:900].strip()
                    last_err = f"{ff} exit {cp.returncode}" + (f": {err_txt}" if err_txt else "")
                    continue
                wav = Path(path_out).read_bytes()
                if not _is_valid_wav(wav):
                    last_err = f"{ff}: output WAV non valido o troppo corto"
                    continue
                return wav, None
            except FileNotFoundError:
                last_err = f"{ff}: eseguibile non trovato"
                continue
            except subprocess.TimeoutExpired:
                last_err = f"{ff}: timeout conversione"
                continue
            except Exception as e:
                last_err = f"{ff}: {e}"
                continue
        return audio_bytes, last_err or "nessun ffmpeg ha funzionato"
    finally:
        if path_in:
            Path(path_in).unlink(missing_ok=True)
        if path_out:
            Path(path_out).unlink(missing_ok=True)


def prepare_audio_for_stt_api(
    audio_bytes: bytes,
    format_hint: Optional[str] = None,
) -> Tuple[bytes, str]:
    """
    Restituisce (bytes, estensione) da passare all'API STT. Preferisce WAV 16k mono.
    """
    if not audio_bytes or len(audio_bytes) < 100:
        return audio_bytes, "wav"

    detected = detect_container(audio_bytes)
    coerced = coerce_format_hint(format_hint)
    ext = detected if detected != "unknown" else (coerced or "webm")

    # Magic bytes battono un MIME sbagliato (es. Safari invia MP4 ma hint webm)
    if detected != "unknown" and coerced and detected != coerced:
        ext = detected

    if ext == "wav" and _is_valid_wav(audio_bytes):
        return audio_bytes, "wav"

    # Ordine: formato atteso, poi fallback comuni browser
    order: list[str] = []
    for x in (ext, "webm", "mp4", "mp3", "ogg", "wav"):
        if x not in order:
            order.append(x)

    last_err: Optional[str] = None
    for suffix in order:
        wav_bytes, err = ffmpeg_bytes_to_wav(audio_bytes, suffix)
        last_err = err
        if err is None and _is_valid_wav(wav_bytes):
            return wav_bytes, "wav"

    raise RuntimeError(
        "Audio dal browser non convertibile in WAV per STT. "
        "Sul server: pip install -U imageio-ffmpeg e/o sudo apt install -y ffmpeg. "
        f"Dettaglio: {last_err or 'conversione fallita'}"
    )


# Back-compat con import esistenti
def webm_to_wav(audio_bytes: bytes) -> Tuple[bytes, Optional[str]]:
    """Alias: solo WebM -> WAV (usato da test o codice legacy)."""
    return ffmpeg_bytes_to_wav(audio_bytes, "webm")
