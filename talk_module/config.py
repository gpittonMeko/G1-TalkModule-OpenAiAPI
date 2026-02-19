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
    tts_voice: str = _str(os.getenv("TTS_VOICE", "shimmer"))
    tts_language: str = _str(os.getenv("TTS_LANGUAGE", "it"))
    whisper_prompt: str = _str(
        os.getenv("WHISPER_PROMPT"),
        "Trascrivi solo le parole pronunciate. Italiano. Mai sottotitoli, Amara, QTSS o testo inventato.",
    )

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
        if self.sample_rate not in (8000, 16000, 44100, 48000):
            errors.append("SAMPLE_RATE deve essere 8000, 16000, 44100 o 48000")
        return errors


settings = Settings()
