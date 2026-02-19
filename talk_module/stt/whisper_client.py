"""
Client OpenAI Whisper per Speech-to-Text.
"""

import subprocess
import tempfile
from io import BytesIO
from pathlib import Path
from typing import Optional, Tuple

from openai import OpenAI

from talk_module.config import settings


def _webm_to_wav(audio_bytes: bytes) -> Tuple[bytes, Optional[str]]:
    """Converte webm in wav 16kHz mono con ffmpeg. Ritorna (wav_bytes, errore)."""
    try:
        with tempfile.NamedTemporaryFile(suffix=".webm", delete=False) as f_in:
            f_in.write(audio_bytes)
            path_in = f_in.name
        path_out = path_in.replace(".webm", ".wav")
        try:
            subprocess.run(
                [
                    "ffmpeg", "-y", "-i", path_in,
                    "-acodec", "pcm_s16le", "-ar", "16000", "-ac", "1",
                    path_out
                ],
                capture_output=True,
                timeout=10,
                check=True,
            )
            wav = Path(path_out).read_bytes()
            return wav, None
        finally:
            Path(path_in).unlink(missing_ok=True)
            Path(path_out).unlink(missing_ok=True)
    except Exception as e:
        return audio_bytes, str(e)


class WhisperClient:
    """Trascrive audio in testo tramite OpenAI Whisper API."""

    def __init__(self, api_key: Optional[str] = None):
        self.client = OpenAI(api_key=api_key or settings.api_key)

    def transcribe(self, audio_bytes: bytes, language: Optional[str] = None, format_hint: Optional[str] = None) -> str:
        """
        Trascrive audio in testo. format_hint: webm (browser), wav, mp3, etc.
        Per webm da browser: converte in wav 16kHz con ffmpeg per migliore compatibilita.
        Ritorna stringa vuota se audio vuoto o errore.
        """
        if not audio_bytes or len(audio_bytes) < 100:
            return ""
        to_send = audio_bytes
        if format_hint:
            ext = format_hint
        else:
            if audio_bytes[:4] == b"RIFF":
                ext = "wav"
            elif audio_bytes[:3] == b"ID3" or (len(audio_bytes) >= 2 and audio_bytes[:2] == b"\xff\xfb"):
                ext = "mp3"
            else:
                ext = "webm"
        if ext == "webm":
            wav_bytes, err = _webm_to_wav(audio_bytes)
            if err is None and len(wav_bytes) > 100:
                to_send = wav_bytes
                ext = "wav"
            elif err:
                print(f"[Whisper] ffmpeg fallito: {err}, uso webm diretto")
        file = BytesIO(to_send)
        file.name = f"audio.{ext}"
        try:
            kwargs = {
                "model": "whisper-1",
                "file": file,
                "response_format": "text",
            }
            if language or settings.tts_language:
                kwargs["language"] = language or settings.tts_language
            if settings.whisper_prompt and settings.whisper_prompt.strip():
                kwargs["prompt"] = settings.whisper_prompt.strip()
            resp = self.client.audio.transcriptions.create(**kwargs)
            if isinstance(resp, str):
                return resp.strip()
            return getattr(resp, "text", str(resp)).strip()
        except Exception as e:
            print(f"[Whisper] Errore: {e}")
            raise
