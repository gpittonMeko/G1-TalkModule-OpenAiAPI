"""
Riproduzione audio - compatibile ARM (Linux/Windows).
Usa multipli fallback: sounddevice, subprocess ffplay/mpg123, pygame.
"""

import io
import subprocess
import tempfile
from pathlib import Path
from typing import Optional

from talk_module.config import settings


class AudioPlayer:
    """Riproduce audio (WAV, MP3) su dispositivo configurato (cuffie/altoparlante)."""

    def __init__(self, device_id: Optional[int] = None):
        self._backend: Optional[str] = None
        self.device_id = device_id

    def play_bytes(self, audio_bytes: bytes, format_hint: str = "wav") -> bool:
        """
        Riproduce bytes audio (WAV o MP3).
        format_hint: "wav" | "mp3"
        Ritorna True se riproduzione avvenuta.
        """
        if not audio_bytes or len(audio_bytes) < 100:
            return False

        ext = ".wav" if format_hint.lower() == "wav" else ".mp3"
        with tempfile.NamedTemporaryFile(suffix=ext, delete=False) as f:
            f.write(audio_bytes)
            path = Path(f.name)

        try:
            return self._play_file(path, format_hint)
        finally:
            path.unlink(missing_ok=True)

    def _play_file(self, path: Path, format_hint: str) -> bool:
        """Riproduce da file. Se device_id configurato, usa sounddevice (cuffie)."""
        # Se dispositivo specifico (cuffie): sounddevice primo per usarlo
        if self.device_id is not None and self._try_sounddevice(path):
            return True
        # Fallback: ffplay, mpg123, aplay, paplay (default di sistema)
        if self._try_ffplay(path):
            return True
        if format_hint.lower() == "mp3" and self._try_mpg123(path):
            return True
        if format_hint.lower() == "wav" and self._try_aplay(path):
            return True
        if self._try_paplay(path):
            return True
        if self._try_sounddevice(path):
            return True
        if format_hint.lower() == "wav" and self._try_g1_internal_speaker(path):
            return True
        return False

    def _try_g1_internal_speaker(self, path: Path) -> bool:
        """Fallback: cassa interna del robot G1 (AudioClient PlayStream via DDS)."""
        try:
            from talk_module.audio.g1_speaker import play_wav_on_g1

            data = path.read_bytes()
            return play_wav_on_g1(data)
        except Exception:
            return False

    def _try_ffplay(self, path: Path) -> bool:
        """Usa ffplay - nodisp per niente finestra."""
        try:
            subprocess.run(
                ["ffplay", "-nodisp", "-autoexit", "-loglevel", "quiet", str(path)],
                check=True,
                capture_output=True,
                timeout=60,
            )
            return True
        except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired):
            return False

    def _try_mpg123(self, path: Path) -> bool:
        """Usa mpg123 per MP3."""
        try:
            subprocess.run(
                ["mpg123", "-q", str(path)],
                check=True,
                capture_output=True,
                timeout=60,
            )
            return True
        except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired):
            return False

    def _try_aplay(self, path: Path) -> bool:
        """Usa aplay (ALSA) per WAV."""
        try:
            subprocess.run(
                ["aplay", "-q", str(path)],
                check=True,
                capture_output=True,
                timeout=60,
            )
            return True
        except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired):
            return False

    def _try_paplay(self, path: Path) -> bool:
        """Usa paplay (PulseAudio)."""
        try:
            subprocess.run(
                ["paplay", str(path)],
                check=True,
                capture_output=True,
                timeout=60,
            )
            return True
        except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired):
            return False

    def _try_sounddevice(self, path: Path) -> bool:
        """Riproduce con sounddevice su dispositivo configurato (cuffie)."""
        try:
            import sounddevice as sd
            import soundfile as sf
            data, rate = sf.read(str(path), dtype="float32")
            kwargs = {}
            if self.device_id is not None:
                kwargs["device"] = self.device_id
            sd.play(data, rate, **kwargs)
            sd.wait()
            return True
        except Exception:
            return False
