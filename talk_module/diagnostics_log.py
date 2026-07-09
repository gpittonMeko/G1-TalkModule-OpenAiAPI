"""Tagged diagnostic logs for dashboard panels (OpenCV, movement, hands, TTS)."""

from __future__ import annotations

from collections import deque
from threading import Lock
from typing import Literal

Channel = Literal["opencv", "movement", "hand", "tts", "general"]

_CHANNELS: dict[str, str] = {
    "opencv": "[OPENCV]",
    "movement": "[MOVIMENTO]",
    "hand": "[MANI]",
    "tts": "[TTS]",
    "general": "[TALK]",
}

_buf: dict[str, deque[str]] = {k: deque(maxlen=200) for k in _CHANNELS}
_lock = Lock()


def diag_log(channel: Channel, message: str) -> None:
    tag = _CHANNELS.get(channel, "[TALK]")
    line = f"{tag} {message}"
    with _lock:
        _buf[channel].append(line)
        _buf["general"].append(line)
    print(line, flush=True)


def get_lines(channel: Channel | str = "general", max_lines: int = 80) -> list[str]:
    key = channel if channel in _buf else "general"
    n = max(5, min(int(max_lines), 300))
    with _lock:
        rows = list(_buf[key])
    return rows[-n:]


def filter_file_lines(lines: list[str], channel: Channel | str) -> list[str]:
    tag = _CHANNELS.get(str(channel), "")
    extra = {
        "opencv": ("cv2", "camera", "yolo", "v4l", "realsense", "OPENCV"),
        "movement": ("robot-action", "robot-match", "arm_sdk", "MOVIMENTO", "loco"),
        "hand": ("brainco", "hand", "MANI", "grip"),
        "tts": ("TTS", "STT", "LLM", "Whisper", "openai", "tts"),
    }
    keys = extra.get(str(channel), ())
    out: list[str] = []
    for ln in lines:
        if tag in ln or any(k.lower() in ln.lower() for k in keys):
            out.append(ln)
    return out[-80:]
