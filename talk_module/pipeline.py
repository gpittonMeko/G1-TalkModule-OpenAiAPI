"""
Pipeline principale: Microfono -> STT -> LLM -> TTS -> Speaker.
"""

from typing import Optional

from talk_module.audio import AudioRecorder, AudioPlayer
from talk_module.stt import WhisperClient
from talk_module.llm import create_llm_client
from talk_module.tts import TTSClient
from talk_module.config import settings


class TalkPipeline:
    """
    Pipeline voce completa:
    1. Registra da microfono
    2. Trascrive (Whisper)
    3. Risponde (LLM)
    4. Riproduce risposta (TTS)
    """

    def __init__(
        self,
        microphone_id: Optional[int] = None,
        sample_rate: Optional[int] = None,
    ):
        settings.ensure_dirs()
        self.recorder = AudioRecorder(
            sample_rate=sample_rate or settings.sample_rate,
            device_id=microphone_id or settings.microphone_device_id,
        )
        self.player = AudioPlayer()
        self.stt = WhisperClient()
        self.llm = create_llm_client()
        self.tts = TTSClient()

    def run_once(self, duration_seconds: Optional[float] = None) -> bool:
        """
        Esegue un singolo ciclo: registra -> trascrivi -> rispondi -> riproduci.
        Ritorna True se tutto OK.
        """
        duration = duration_seconds or settings.recording_timeout
        print(f"\n[Talk] Registrazione per {duration}s... Parla ora.")
        audio = self.recorder.record_fixed_duration(duration)
        if not audio:
            print("[Talk] Nessun audio registrato.")
            return False

        print("[Talk] Trascrizione...")
        text = self.stt.transcribe(audio)
        if not text:
            print("[Talk] Nessun testo riconosciuto.")
            return False
        print(f"[Talk] Hai detto: {text}")

        print("[Talk] Risposta LLM...")
        response = self.llm.chat(text)
        if not response:
            print("[Talk] Nessuna risposta dal modello.")
            return False
        print(f"[Talk] Risposta: {response}")

        print("[Talk] Sintesi vocale e riproduzione...")
        speech = self.tts.synthesize(response, format="mp3")
        if speech:
            self.player.play_bytes(speech, format_hint="mp3")
        return True

    def run_conversation(
        self,
        duration_seconds: Optional[float] = None,
        max_turns: Optional[int] = None,
    ) -> None:
        """
        Loop conversazione: continua finché l'utente non interrompe (Ctrl+C).
        max_turns: limite turni (None = infinito).
        """
        duration = duration_seconds or settings.recording_timeout
        turns = 0
        try:
            while max_turns is None or turns < max_turns:
                if self.run_once(duration):
                    turns += 1
        except KeyboardInterrupt:
            print("\n[Talk] Interrotto dall'utente.")
