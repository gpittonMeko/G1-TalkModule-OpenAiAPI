"""
Configurazione centralizzata - carica da .env e variabili d'ambiente.
"""

import os
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv

# Carica .env dalla root del progetto
_root = Path(__file__).resolve().parent.parent
load_dotenv(_root / ".env")


def _int(value: str) -> Optional[int]:
    """Converte in int, None se vuoto o non valido."""
    if value is None or value.strip() == "":
        return None
    try:
        return int(value.strip())
    except ValueError:
        return None


def _str(value: str, default: str = "") -> str:
    """Restituisce stringa pulita o default."""
    if value is None or value.strip() == "":
        return default
    return value.strip()


class Settings:
    """Impostazioni del Talk Module."""

    # OpenAI
    api_key: str = _str(os.getenv("OPENAI_API_KEY", ""))
    llm_model: str = _str(os.getenv("LLM_MODEL", "gpt-4o-mini"))

    # STT provider: whisper | deepgram | groq (auto groq se GROQ_API_KEY presente)
    _stt_default = "groq" if _str(os.getenv("GROQ_API_KEY")) else "whisper"
    stt_provider: str = _str(os.getenv("STT_PROVIDER", _stt_default)).lower()
    deepgram_api_key: str = _str(os.getenv("DEEPGRAM_API_KEY", ""))
    groq_api_key: str = _str(os.getenv("GROQ_API_KEY", ""))
    tts_voice: str = _str(os.getenv("TTS_VOICE", "nova"))
    tts_model: str = _str(os.getenv("TTS_MODEL", "gpt-4o-mini-tts"))  # gpt-4o-mini-tts più affidabile per italiano
    tts_language: str = _str(os.getenv("TTS_LANGUAGE", "it"))
    whisper_prompt: str = _str(
        os.getenv("WHISPER_PROMPT"),
        "Lingua italiana. Esempi: prova prova prova, che ore sono, hey g1. Trascrivi solo le parole pronunciate.",
    )
    # STT fuzzy: threshold e min_word_length in config/stt_config.json (opzionale override via .env)
    stt_fuzzy_threshold: float = float(os.getenv("STT_FUZZY_THRESHOLD", "0.72"))
    stt_min_word_length: int = _int(os.getenv("STT_MIN_WORD_LENGTH", "3")) or 3

    # Quick lookup (ora, meteo, domande fattuali)
    quick_lookup_enabled: bool = os.getenv("QUICK_LOOKUP_ENABLED", "true").lower() in ("1", "true", "yes")
    quick_lookup_timeout: int = _int(os.getenv("QUICK_LOOKUP_TIMEOUT", "3")) or 3
    default_weather_city: str = _str(os.getenv("DEFAULT_WEATHER_CITY", "Rome"))

    # Audio
    sample_rate: int = _int(os.getenv("SAMPLE_RATE", "16000")) or 16000
    microphone_device_id: Optional[int] = _int(os.getenv("MICROPHONE_DEVICE_ID"))
    recording_timeout: float = float(os.getenv("RECORDING_TIMEOUT", "10"))

    # Paths
    temp_dir: Path = _root / "temp"
    audio_dir: Path = temp_dir / "audio"

    def ensure_dirs(self) -> None:
        """Crea le directory temporanee se non esistono."""
        self.audio_dir.mkdir(parents=True, exist_ok=True)

    def validate(self) -> list[str]:
        """Valida le impostazioni, ritorna lista errori."""
        errors = []
        if not self.api_key:
            errors.append("OPENAI_API_KEY non configurata. Imposta in .env")
        if self.stt_provider == "deepgram" and not self.deepgram_api_key:
            errors.append("STT_PROVIDER=deepgram richiede DEEPGRAM_API_KEY in .env")
        if self.sample_rate not in (8000, 16000, 44100, 48000):
            errors.append("SAMPLE_RATE deve essere 8000, 16000, 44100 o 48000")
        return errors


settings = Settings()
