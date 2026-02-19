"""
Client OpenAI TTS per Text-to-Speech.
"""

from typing import Optional

from openai import OpenAI

from talk_module.config import settings


class TTSClient:
    """Sintetizza testo in audio tramite OpenAI TTS API."""

    def __init__(self, api_key: Optional[str] = None, voice: Optional[str] = None):
        self.client = OpenAI(api_key=api_key or settings.api_key)
        self.voice = voice or settings.tts_voice

    def synthesize(self, text: str, format: str = "mp3") -> bytes:
        """
        Converte testo in audio.
        Ritorna bytes MP3 (o formato richiesto).
        """
        if not text or not text.strip():
            return b""
        try:
            resp = self.client.audio.speech.create(
                model="tts-1",
                voice=self.voice,
                input=text.strip(),
                response_format=format,
                speed=1.5,
            )
            return resp.content
        except Exception as e:
            print(f"[TTS] Errore: {e}")
            return b""
