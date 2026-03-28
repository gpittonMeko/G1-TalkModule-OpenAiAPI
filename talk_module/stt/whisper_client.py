"""
Client OpenAI Whisper per Speech-to-Text.
"""

from io import BytesIO
from typing import Optional

from openai import OpenAI

from talk_module.config import settings
from talk_module.stt.audio_convert import prepare_audio_for_stt_api


class WhisperClient:
    """Trascrive audio in testo tramite OpenAI Transcription API (whisper-1 / gpt-4o-transcribe / gpt-4o-mini-transcribe)."""

    def __init__(self, api_key: Optional[str] = None):
        self.client = OpenAI(api_key=api_key or settings.api_key)
        self.model = settings.stt_model or "gpt-4o-mini-transcribe"

    def transcribe(self, audio_bytes: bytes, language: Optional[str] = None, format_hint: Optional[str] = None, prompt: Optional[str] = None, model: Optional[str] = None) -> str:
        """
        Trascrive audio in testo. format_hint: MIME browser (audio/webm, audio/mp4, …) o estensione.
        Container rilevato dai magic bytes; conversione in WAV 16k con ffmpeg (imageio-ffmpeg).
        prompt: override per settings.whisper_prompt (utile per wake word detection).
        model: override per self.model (es. wake_stt_model per wake detection).
        """
        if not audio_bytes or len(audio_bytes) < 100:
            return ""
        to_send, ext = prepare_audio_for_stt_api(audio_bytes, format_hint)
        file = BytesIO(to_send)
        file.name = f"audio.{ext}"
        file.seek(0)
        effective_model = model or self.model
        try:
            kwargs = {
                "model": effective_model,
                "file": file,
                "response_format": "text",
            }
            if language or settings.tts_language:
                kwargs["language"] = language or settings.tts_language
            effective_prompt = prompt if prompt is not None else (settings.whisper_prompt or "").strip()
            if effective_prompt:
                kwargs["prompt"] = effective_prompt
            resp = self.client.audio.transcriptions.create(**kwargs)
            if isinstance(resp, str):
                return resp.strip()
            return getattr(resp, "text", str(resp)).strip()
        except Exception as e:
            print(f"[STT:{effective_model}] Errore: {e}")
            raise
