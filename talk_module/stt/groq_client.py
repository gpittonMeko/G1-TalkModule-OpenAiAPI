"""
Client Groq Whisper per Speech-to-Text.
Alternativa veloce e gratuita: stesso modello Whisper, inferenza molto più rapida.
Supporta webm, wav, mp3. API compatibile OpenAI.
"""

from io import BytesIO
from typing import Optional

from talk_module.config import settings


class GroqWhisperClient:
    """Trascrive audio in testo tramite Groq Whisper API (veloce, gratuito)."""

    def __init__(self, api_key: Optional[str] = None):
        self._api_key = api_key or getattr(settings, "groq_api_key", None) or ""
        self._client = None

    def _get_client(self):
        if self._client is None:
            try:
                from groq import Groq
                self._client = Groq(api_key=self._api_key)
            except ImportError:
                raise RuntimeError("Installa: pip install groq")
        return self._client

    def transcribe(
        self,
        audio_bytes: bytes,
        language: Optional[str] = None,
        format_hint: Optional[str] = None,
    ) -> str:
        """
        Trascrive audio in testo. Accetta webm, wav, mp3.
        Ritorna stringa vuota se audio vuoto o errore.
        """
        if not audio_bytes or len(audio_bytes) < 100:
            return ""
        ext = format_hint or "webm"
        if not format_hint:
            if audio_bytes[:4] == b"RIFF":
                ext = "wav"
            elif audio_bytes[:3] == b"ID3" or (
                len(audio_bytes) >= 2 and audio_bytes[:2] == b"\xff\xfb"
            ):
                ext = "mp3"
        file = BytesIO(audio_bytes)
        file.name = f"audio.{ext}"
        try:
            client = self._get_client()
            kwargs = {
                "file": file,
                "model": "whisper-large-v3-turbo",
                "response_format": "text",
            }
            if language or settings.tts_language:
                kwargs["language"] = language or settings.tts_language
            if settings.whisper_prompt and settings.whisper_prompt.strip():
                kwargs["prompt"] = settings.whisper_prompt.strip()
            resp = client.audio.transcriptions.create(**kwargs)
            if isinstance(resp, str):
                return resp.strip()
            return getattr(resp, "text", str(resp)).strip()
        except Exception as e:
            print(f"[Groq] Errore: {e}")
            raise
