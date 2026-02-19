"""Modulo audio: registrazione, riproduzione, selezione dispositivi.
Richiede PortAudio: sudo apt install portaudio19-dev (vedi scripts/install_audio_jetson.sh)
"""

from talk_module.audio.recorder import AudioRecorder
from talk_module.audio.player import AudioPlayer
from talk_module.audio.device_utils import list_audio_devices

_AUDIO_AVAILABLE = True
__all__ = ["AudioRecorder", "AudioPlayer", "list_audio_devices", "_AUDIO_AVAILABLE"]
