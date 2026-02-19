"""
Registrazione audio da microfono.
Usa sounddevice (PortAudio) - compatibile ARM.
"""

import io
from typing import Optional

import numpy as np
import sounddevice as sd
import soundfile as sf

from talk_module.audio.device_utils import get_device_supporting_input
from talk_module.config import settings

# Sample rate da provare (molti USB non supportano 16kHz)
_RATES_TO_TRY = (16000, 44100, 48000, 22050, 32000)


def _get_device_sample_rate(device_id: Optional[int]) -> int:
    """Ritorna il sample rate di default del dispositivo, o 44100."""
    if device_id is not None:
        try:
            dev = sd.query_devices(device_id)
            rate = dev.get("default_samplerate")
            if rate and rate > 0:
                return int(rate)
        except Exception:
            pass
    return 44100


class AudioRecorder:
    """Registra audio dal microfono in formato WAV compatibile Whisper."""

    def __init__(
        self,
        sample_rate: Optional[int] = None,
        device_id: Optional[int] = None,
        channels: int = 1,
    ):
        self.device_id = get_device_supporting_input(
            device_id or settings.microphone_device_id,
            sample_rate or settings.sample_rate,
        )
        # Priorità: device default (USB spesso 44100/48000) > config
        self.sample_rate = sample_rate or settings.sample_rate or _get_device_sample_rate(self.device_id)
        self.channels = channels

    def record_until_stop(self, stop_check, chunk_duration: float = 0.3, min_duration: float = 0.5) -> bytes:
        """Registra finché stop_check() ritorna True. Push-to-talk."""
        preferred = _get_device_sample_rate(self.device_id)
        rates = (preferred, self.sample_rate) + tuple(r for r in _RATES_TO_TRY if r not in (preferred, self.sample_rate))
        for try_rate in rates:
            try:
                self.sample_rate = try_rate
                chunk_samples = int(chunk_duration * try_rate)
                buffer = []
                while not stop_check():
                    rec = sd.rec(chunk_samples, samplerate=try_rate, channels=self.channels, device=self.device_id, dtype="float32")
                    sd.wait()
                    buffer.append(rec.squeeze())
                if not buffer:
                    return b""
                audio = np.concatenate(buffer)
                total_dur = len(audio) / try_rate
                if total_dur < min_duration:
                    return b""
                return self._to_wav_bytes(audio, try_rate)
            except sd.PortAudioError as e:
                if "-9997" in str(e) or "Invalid sample rate" in str(e):
                    continue
                raise
        return b""

    def record_fixed_duration(self, duration_seconds: float) -> bytes:
        """Registra per una durata fissa. Prova più sample rate se -9997 (Invalid sample rate)."""
        # Prova prima il rate del device, poi altri comuni
        preferred = _get_device_sample_rate(self.device_id)
        rates = (preferred, self.sample_rate) + tuple(r for r in _RATES_TO_TRY if r not in (preferred, self.sample_rate))
        last_err = None
        for try_rate in rates:
            try:
                recording = sd.rec(
                    int(duration_seconds * try_rate),
                    samplerate=try_rate,
                    channels=self.channels,
                    device=self.device_id,
                    dtype="float32",
                )
                sd.wait()
                self.sample_rate = try_rate
                return self._to_wav_bytes(recording.squeeze())
            except sd.PortAudioError as e:
                last_err = e
                if "-9997" in str(e) or "Invalid sample rate" in str(e):
                    continue
                raise
        raise last_err or sd.PortAudioError("Nessun sample rate supportato")

    def _to_wav_bytes(self, audio: np.ndarray, sample_rate: Optional[int] = None) -> bytes:
        """Converte numpy array in bytes WAV."""
        buf = io.BytesIO()
        audio_flat = audio.squeeze().astype(np.float32)
        rate = sample_rate or self.sample_rate
        sf.write(buf, audio_flat, rate, format="WAV")
        return buf.getvalue()

    def record_until_silence(
        self,
        silence_seconds: float = 10.0,
        chunk_duration: float = 0.5,
        silence_threshold: float = 0.01,
        max_duration: float = 120.0,
        stop_check=None,
    ):
        """
        Registra finché non c'è silenzio per silence_seconds.
        Yield audio bytes quando rileva fine enunciato (speech + silence_seconds).
        stop_check: callable che ritorna True per fermare.
        """
        preferred = _get_device_sample_rate(self.device_id)
        rates = (preferred, self.sample_rate) + tuple(r for r in _RATES_TO_TRY if r not in (preferred, self.sample_rate))
        for try_rate in rates:
            try:
                yield from self._record_until_silence_impl(
                    try_rate, silence_seconds, chunk_duration, silence_threshold, max_duration, stop_check
                )
                return
            except sd.PortAudioError as e:
                if "-9997" in str(e) or "Invalid sample rate" in str(e):
                    continue
                raise
        raise sd.PortAudioError("Nessun sample rate supportato")

    def _record_until_silence_impl(
        self, rate: int, silence_sec: float, chunk_dur: float, threshold: float, max_dur: float, stop_check=None
    ):
        self.sample_rate = rate
        silence_chunks_needed = int(silence_sec / chunk_dur)
        buffer = []
        silence_count = 0
        in_speech = False
        total_dur = 0.0
        chunk_samples = int(chunk_dur * rate)

        while stop_check is None or not stop_check():
            rec = sd.rec(chunk_samples, samplerate=rate, channels=self.channels, device=self.device_id, dtype="float32")
            sd.wait()
            chunk = rec.squeeze()
            rms = float(np.sqrt(np.mean(chunk**2)))
            total_dur += chunk_dur

            if rms > threshold:
                in_speech = True
                silence_count = 0
                buffer.append(chunk)
            else:
                if in_speech:
                    buffer.append(chunk)
                    silence_count += 1
                    if silence_count >= silence_chunks_needed:
                        if buffer and len(buffer) > 2:
                            audio = np.concatenate(buffer)
                            yield self._to_wav_bytes(audio, rate)
                        buffer = []
                        silence_count = 0
                        in_speech = False
                        total_dur = 0
                else:
                    pass

            if total_dur >= max_dur and buffer:
                audio = np.concatenate(buffer)
                yield self._to_wav_bytes(audio, rate)
                buffer = []
                total_dur = 0
