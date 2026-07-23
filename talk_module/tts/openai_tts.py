"""
Client OpenAI TTS per Text-to-Speech.
"""

import subprocess
import tempfile
from pathlib import Path
from typing import Optional

from talk_module.config import settings
from talk_module.openai_http import make_openai_client


def _find_ffmpeg() -> Optional[str]:
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        pass
    for p in ("ffmpeg", "/usr/bin/ffmpeg", "/usr/local/bin/ffmpeg"):
        try:
            subprocess.run([p, "-version"], capture_output=True, timeout=5)
            return p
        except Exception:
            continue
    return None


def _loudnorm_mp3(audio_bytes: bytes) -> bytes:
    """Apply EBU R128 loudness normalization via FFmpeg. Returns original on failure."""
    if not audio_bytes or len(audio_bytes) < 200:
        return audio_bytes
    ffmpeg = _find_ffmpeg()
    if not ffmpeg:
        return audio_bytes
    try:
        with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as fin:
            fin.write(audio_bytes)
            fin_path = fin.name
        fout_path = fin_path.replace(".mp3", "_loud.mp3")
        r = subprocess.run(
            [ffmpeg, "-y", "-i", fin_path,
             "-af", "loudnorm=I=-14:TP=-1:LRA=11,volume=1.5",
             "-b:a", "128k", fout_path],
            capture_output=True, timeout=15,
        )
        if r.returncode == 0 and Path(fout_path).exists():
            result = Path(fout_path).read_bytes()
            if len(result) > 100:
                return result
    except Exception as e:
        print(f"[TTS loudnorm] {e}", flush=True)
    finally:
        for p in (fin_path, fout_path):
            try:
                Path(p).unlink(missing_ok=True)
            except Exception:
                pass
    return audio_bytes


class TTSClient:
    """Sintetizza testo in audio tramite OpenAI TTS API."""

    def __init__(self, api_key: Optional[str] = None, voice: Optional[str] = None):
        self.client = make_openai_client(api_key or settings.api_key)
        self.voice = voice or settings.tts_voice

    def synthesize(self, text: str, format: str = "mp3") -> bytes:
        """
        Converte testo in audio.
        Ritorna bytes MP3 (o formato richiesto).
        gpt-4o-mini-tts: più affidabile per italiano (tts-1-hd spesso sbaglia).
        """
        if not text or not text.strip():
            return b""
        try:
            kwargs = {
                "model": settings.tts_model,
                "voice": self.voice,
                "input": text.strip(),
                "response_format": format,
                "speed": settings.tts_speed,
            }
            if "gpt-4o-mini-tts" in settings.tts_model:
                kwargs["instructions"] = settings.tts_instructions
            try:
                resp = self.client.audio.speech.create(**kwargs)
            except TypeError:
                kwargs.pop("instructions", None)
                resp = self.client.audio.speech.create(**kwargs)
            raw = resp.content
            if raw and format == "mp3" and not settings.tts_skip_loudnorm:
                raw = _loudnorm_mp3(raw)
            return raw
        except Exception as e:
            print(f"[TTS] Errore: {e}")
            return b""
