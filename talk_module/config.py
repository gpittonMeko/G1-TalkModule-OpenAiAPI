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


def _openai_max_retries_from_env() -> int:
    v = _int(os.getenv("OPENAI_MAX_RETRIES", "1"))
    return 1 if v is None else max(0, v)


class Settings:
    """Impostazioni del Talk Module."""

    # OpenAI
    api_key: str = _str(os.getenv("OPENAI_API_KEY", ""))
    stt_model: str = _str(os.getenv("STT_MODEL", "gpt-4o-transcribe"))
    wake_stt_model: str = _str(os.getenv("WAKE_STT_MODEL", "")) or _str(os.getenv("STT_MODEL", "gpt-4o-transcribe"))
    # LLM: openai (default) | gemini
    llm_provider: str = _str(os.getenv("LLM_PROVIDER", "openai")).lower()
    gemini_api_key: str = _str(os.getenv("GEMINI_API_KEY", ""))
    gemini_model: str = _str(os.getenv("GEMINI_MODEL", "gemini-2.5-flash"))
    gemini_thinking_budget: int = _int(os.getenv("GEMINI_THINKING_BUDGET", "0")) or 0
    llm_fallback_enabled: bool = os.getenv("LLM_FALLBACK_ENABLED", "true").lower() in ("1", "true", "yes")
    llm_fallback_model: str = _str(os.getenv("LLM_FALLBACK_MODEL", ""))
    llm_model: str = _str(os.getenv("LLM_MODEL", "gpt-5.4-mini"))
    llm_text_model: str = _str(os.getenv("LLM_TEXT_MODEL", "")) or _str(os.getenv("LLM_MODEL", "gpt-5.4-mini"))
    # Limite token risposta (max_completion_tokens / max_tokens a seconda del modello)
    llm_max_completion_tokens: int = _int(os.getenv("LLM_MAX_COMPLETION_TOKENS", "1024")) or 1024
    # Voce: risposte corte = meno latenza LLM + TTS
    llm_voice_max_tokens: int = _int(os.getenv("LLM_VOICE_MAX_TOKENS", "220")) or 220
    llm_voice_max_completion_tokens: int = llm_voice_max_tokens

    # STT: whisper = OpenAI Whisper API (stessa chiave OPENAI_API_KEY). groq/deepgram solo se imposti STT_PROVIDER.
    stt_provider: str = _str(os.getenv("STT_PROVIDER", "whisper")).lower()
    deepgram_api_key: str = _str(os.getenv("DEEPGRAM_API_KEY", ""))
    groq_api_key: str = _str(os.getenv("GROQ_API_KEY", ""))
    tts_voice: str = _str(os.getenv("TTS_VOICE", "onyx"))
    tts_voice_robot: str = _str(os.getenv("TTS_VOICE_ROBOT", "echo"))  # voce più metallica per traccia robot
    tts_model: str = _str(os.getenv("TTS_MODEL", "gpt-4o-mini-tts"))  # gpt-4o-mini-tts più affidabile per italiano
    tts_instructions: str = _str(
        os.getenv("TTS_INSTRUCTIONS"),
        "Parla in italiano con voce maschile naturale, chiara e alta. Pronuncia correttamente ogni parola.",
    )
    tts_loudnorm: bool = os.getenv("TTS_LOUDNORM", "0").lower() in ("1", "true", "yes")
    tts_skip_loudnorm: bool = os.getenv("TTS_SKIP_LOUDNORM", "1").lower() in ("1", "true", "yes")
    tts_speed: float = float(os.getenv("TTS_SPEED", "1.05"))
    robot_effect_preset: str = _str(os.getenv("ROBOT_EFFECT_PRESET", "robot_full"))  # telephone|ring_mod|bitcrush|robot_full
    tts_language: str = _str(os.getenv("TTS_LANGUAGE", "it"))
    whisper_prompt: str = _str(
        os.getenv("WHISPER_PROMPT"),
        "Trascrizione fedele in italiano. NON tradurre in inglese. NON inventare sottotitoli o frasi inglesi. "
        "L'utente parla italiano con un robot G1. Esempi: hey g1, ehi g1, buonasera, che ore sono, "
        "spiega, grazie, fai un passo avanti. Scrivi solo ciò che viene detto, in italiano.",
    )
    # Endpointing ascolto continuo browser (ms) e Jetson /ws/listen (secondi)
    stt_cmd_silence_ms: int = _int(os.getenv("STT_CMD_SILENCE_MS", "2800")) or 2800
    stt_cmd_slice_ms: int = _int(os.getenv("STT_CMD_SLICE_MS", "20000")) or 20000
    stt_wake_slice_ms: int = _int(os.getenv("STT_WAKE_SLICE_MS", "6000")) or 6000
    stt_cmd_min_voice_ms: int = _int(os.getenv("STT_CMD_MIN_VOICE_MS", "400")) or 400
    stt_listen_silence_sec: float = float(os.getenv("STT_LISTEN_SILENCE_SEC", "2.8"))
    # STT fuzzy: threshold e min_word_length in config/stt_config.json (opzionale override via .env)
    stt_fuzzy_threshold: float = float(os.getenv("STT_FUZZY_THRESHOLD", "0.85"))
    stt_min_word_length: int = _int(os.getenv("STT_MIN_WORD_LENGTH", "3")) or 3

    # Quick lookup (ora, meteo, domande fattuali)
    quick_lookup_enabled: bool = os.getenv("QUICK_LOOKUP_ENABLED", "true").lower() in ("1", "true", "yes")
    quick_lookup_timeout: int = _int(os.getenv("QUICK_LOOKUP_TIMEOUT", "3")) or 3
    default_weather_city: str = _str(os.getenv("DEFAULT_WEATHER_CITY", "Rome"))

    # Profilo ospiti/visita (config/visitor_profiles.json): es. alpitronic
    visitor_profile: str = _str(os.getenv("G1_VISITOR_PROFILE", ""))

    # Wake word: risposta TTS quando l'utente dice solo "Hey G1" senza domanda
    hey_g1_ack_text: str = _str(
        os.getenv("HEY_G1_ACK_TEXT"),
        "Dimmi pure.",
    )

    # Audio
    sample_rate: int = _int(os.getenv("SAMPLE_RATE", "16000")) or 16000
    microphone_device_id: Optional[int] = _int(os.getenv("MICROPHONE_DEVICE_ID"))
    recording_timeout: float = float(os.getenv("RECORDING_TIMEOUT", "10"))

    # OpenAI HTTP: default brevi — se l'API non risponde, errore netto (no attese lunghissime). Override .env se serve.
    openai_connect_timeout: float = float(os.getenv("OPENAI_CONNECT_TIMEOUT", "20"))
    openai_read_timeout: float = float(os.getenv("OPENAI_READ_TIMEOUT", "60"))
    openai_max_retries: int = _openai_max_retries_from_env()  # 0 = nessun retry

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
        if self.llm_provider == "gemini" and not self.gemini_api_key:
            errors.append("LLM_PROVIDER=gemini richiede GEMINI_API_KEY in .env")
        if self.llm_provider not in ("openai", "gemini"):
            errors.append("LLM_PROVIDER deve essere openai o gemini")
        if self.stt_provider == "deepgram" and not self.deepgram_api_key:
            errors.append("STT_PROVIDER=deepgram richiede DEEPGRAM_API_KEY in .env")
        if self.sample_rate not in (8000, 16000, 44100, 48000):
            errors.append("SAMPLE_RATE deve essere 8000, 16000, 44100 o 48000")
        return errors


settings = Settings()
