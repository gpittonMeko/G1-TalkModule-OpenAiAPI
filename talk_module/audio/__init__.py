"""Modulo audio: registrazione, riproduzione, selezione dispositivi.
Richiede PortAudio per registrazione: sudo apt install portaudio19-dev
"""

from talk_module.audio.player import AudioPlayer

try:
    from talk_module.audio.recorder import AudioRecorder
    from talk_module.audio.device_utils import list_audio_devices

    _AUDIO_AVAILABLE = True
except OSError:
    AudioRecorder = None  # type: ignore[misc, assignment]

    def list_audio_devices():  # type: ignore[misc]
        return []

    _AUDIO_AVAILABLE = False

__all__ = ["AudioRecorder", "AudioPlayer", "list_audio_devices", "_AUDIO_AVAILABLE"]
