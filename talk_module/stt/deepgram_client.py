"""
Client Deepgram Nova per Speech-to-Text.
Alternativa a Whisper: meno allucinazioni, WER migliore, $200 crediti gratuiti.
"""

from typing import Optional

from talk_module.config import settings


class DeepgramClient:
    """Trascrive audio in testo tramite Deepgram Nova API."""

    def __init__(self, api_key: Optional[str] = None):
        self._api_key = api_key or getattr(settings, "deepgram_api_key", None) or ""
        self._client = None

    def _get_client(self):
        if self._client is None:
            try:
                from deepgram import DeepgramClient as DG
                self._client = DG(api_key=self._api_key) if self._api_key else DG()
            except ImportError:
                raise RuntimeError("Installa: pip install deepgram-sdk")
        return self._client

    def transcribe(
        self,
        audio_bytes: bytes,
        language: Optional[str] = None,
        format_hint: Optional[str] = None,
        prompt: Optional[str] = None,
    ) -> str:
        """
        Trascrive audio in testo. Accetta webm, wav, mp3.
        prompt: ignored for Deepgram (uses keywords instead).
        Ritorna stringa vuota se audio vuoto o errore.
        """
        if not audio_bytes or len(audio_bytes) < 100:
            return ""
        try:
            client = self._get_client()
            kw = {}
            if prompt and "g1" in prompt.lower():
                kw["keywords"] = ["Hey G1:2", "G1:2", "Ehi G1:2"]
            resp = client.listen.v1.media.transcribe_file(
                request=audio_bytes,
                model="nova-3",
                language=language or settings.tts_language or "it",
                smart_format=True,
                **kw,
            )
            text = ""
            if resp and hasattr(resp, "results") and resp.results:
                channels = getattr(resp.results, "channels", None)
                if channels and len(channels) > 0:
                    alts = getattr(channels[0], "alternatives", None)
                    if alts and len(alts) > 0:
                        text = (getattr(alts[0], "transcript", None) or "").strip()
            return text
        except Exception as e:
            print(f"[Deepgram] Errore: {e}")
            raise
