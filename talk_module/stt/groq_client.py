"""
Client Groq Whisper per Speech-to-Text.
Alternativa veloce e gratuita: stesso modello Whisper, inferenza molto più rapida.
"""

from io import BytesIO
from typing import Optional

from talk_module.config import settings
from talk_module.stt.audio_convert import prepare_audio_for_stt_api


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
        prompt: Optional[str] = None,
    ) -> str:
        """Trascrive audio. Stessa pipeline di OpenAI: WAV 16k da qualsiasi container browser.
        prompt: override per settings.whisper_prompt (utile per wake word detection)."""
        if not audio_bytes or len(audio_bytes) < 100:
            return ""
        to_send, ext = prepare_audio_for_stt_api(audio_bytes, format_hint)
        file = BytesIO(to_send)
        file.name = f"audio.{ext}"
        file.seek(0)
        try:
            client = self._get_client()
            kwargs = {
                "file": file,
                "model": "whisper-large-v3-turbo",
                "response_format": "text",
            }
            if language or settings.tts_language:
                kwargs["language"] = language or settings.tts_language
            effective_prompt = prompt if prompt is not None else (settings.whisper_prompt or "").strip()
            if effective_prompt:
                kwargs["prompt"] = effective_prompt
            resp = client.audio.transcriptions.create(**kwargs)
            if isinstance(resp, str):
                return resp.strip()
            return getattr(resp, "text", str(resp)).strip()
        except Exception as e:
            print(f"[Groq] Errore: {e}")
            raise
