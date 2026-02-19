"""
Utility per elencare dispositivi audio.
- Integrati (dentro la macchina: G1, laptop, Jetson)
- USB / Jack (esterni)
- Bluetooth (se disponibile)
- Rete WiFi (client web)
"""

import subprocess
from typing import Optional

# Etichette per tipo dispositivo (per l'utente)
TYPE_LABELS = {
    "builtin": "Integrato",
    "usb": "USB",
    "jack": "Jack",
    "bluetooth": "Bluetooth",
    "hdmi": "HDMI",
    "virtual": "Virtuale",
    "headphone": "Cuffie",
    "speaker": "Altoparlante",
    "other": "Altro",
}


def _device_type(name: str, has_input: bool, has_output: bool) -> str:
    """Ritorna: builtin, usb, bluetooth, jack, headphone, speaker, hdmi, virtual, other."""
    n = name.lower()
    if "usb" in n:
        return "usb"
    if "bluetooth" in n or "bt " in n or "bluez" in n:
        return "bluetooth"
    if "hdmi" in n or "digital output" in n or "s/pdif" in n:
        return "hdmi"
    if "pulse" in n or "pipewire" in n or "sysdefault" in n:
        return "virtual"
    if n.strip() == "default":
        return "virtual"
    if any(x in n for x in ("headphone", "headset", "cuffie", "auricolari", "earphone")):
        return "headphone"
    if "speaker" in n or "altoparlante" in n:
        return "speaker" if "head" not in n else "headphone"
    if any(x in n for x in ("built-in", "internal", "array", "integrated", "onboard")):
        return "builtin"
    if "realtek" in n or ("intel" in n and "smart" in n):
        return "builtin"
    if "analog" in n or "line out" in n:
        return "builtin" if "external" not in n else "jack"
    return "other"


def _is_physical_mic(name: str) -> bool:
    """Esclude solo dispositivi chiaramente virtuali."""
    n = name.lower()
    skip = ("null", "monitor", "loopback", "stereo mix", "vb-", "virtual")
    if any(s in n for s in skip):
        return False
    if n.strip() in ("default", "sysdefault") and "usb" not in n:
        return False
    return True


def _is_physical_speaker(name: str) -> bool:
    """Esclude solo dispositivi chiaramente virtuali."""
    n = name.lower()
    skip = ("null", "monitor", "loopback", "vb-", "virtual")
    if any(s in n for s in skip):
        return False
    if n.strip() in ("default", "sysdefault") and "usb" not in n:
        return False
    return True


def _is_default(dev, idx: int, i: int) -> bool:
    """Controlla se i e' il default device (compatibile con _InputOutputPair)."""
    if dev is None:
        return False
    try:
        v = dev[idx]
        return i == v
    except (TypeError, IndexError, KeyError, AttributeError):
        return False


def list_audio_devices() -> list[dict]:
    """Elenca dispositivi con metadati per filtro."""
    import sounddevice as sd
    devices = sd.query_devices()
    result = []
    seen = set()
    for i, dev in enumerate(devices):
        name = dev.get("name", "Unknown")
        inp = dev.get("max_input_channels", 0)
        out = dev.get("max_output_channels", 0)
        dtype = _device_type(name, inp > 0, out > 0)
        if name in seen and dtype == "virtual":
            continue
        seen.add(name)
        result.append({
            "index": i,
            "name": name,
            "input_channels": inp,
            "output_channels": out,
            "sample_rate": dev.get("default_samplerate", 0),
            "hostapi": dev.get("hostapi", 0),
            "is_default_input": _is_default(sd.default.device, 0, i),
            "is_default_output": _is_default(sd.default.device, 1, i),
            "device_type": dtype,
            "is_physical_mic": inp > 0 and _is_physical_mic(name),
            "is_physical_speaker": out > 0 and _is_physical_speaker(name),
        })
    return result


def list_microphones(physical_only: bool = True) -> list[dict]:
    """Lista microfoni: integrati, USB, bluetooth, jack."""
    devs = [d for d in list_audio_devices() if d.get("input_channels", 0) > 0]
    if physical_only:
        physical = [d for d in devs if d.get("is_physical_mic", True)]
        if physical:
            devs = physical
    # Ordina: integrati > USB > Bluetooth > Jack > altro > virtual
    order = {"builtin": 0, "usb": 1, "bluetooth": 2, "jack": 3, "other": 4, "hdmi": 5, "virtual": 6}
    devs.sort(key=lambda d: (order.get(d.get("device_type", ""), 7), -d.get("input_channels", 0)))
    return devs


def list_speakers(physical_only: bool = True) -> list[dict]:
    """Lista altoparlanti: integrati, USB, bluetooth, cuffie."""
    devs = [d for d in list_audio_devices() if d.get("output_channels", 0) > 0]
    if physical_only:
        physical = [d for d in devs if d.get("is_physical_speaker", True)]
        if physical:
            devs = physical
    # Ordina: integrati > USB > Bluetooth > Jack > altro > virtual
    order = {"builtin": 0, "usb": 1, "bluetooth": 2, "jack": 3, "headphone": 2, "speaker": 2, "other": 4, "hdmi": 5, "virtual": 6}
    devs.sort(key=lambda d: (order.get(d.get("device_type", ""), 7), -d.get("output_channels", 0)))
    return devs


def list_bluetooth_devices_available() -> list[dict]:
    """
    Lista dispositivi Bluetooth accoppiati (cuffie, speaker).
    Se connessi, appaiono anche in PortAudio. Qui mostriamo cosa è disponibile da collegare.
    """
    result = []
    try:
        out = subprocess.run(
            ["bluetoothctl", "devices"],
            capture_output=True,
            text=True,
            timeout=3,
        )
        if out.returncode != 0:
            return result
        for line in out.stdout.strip().split("\n"):
            if "Device" in line:
                parts = line.split(" ", 2)
                if len(parts) >= 3:
                    mac = parts[1]
                    name = (parts[2].strip() or f"Bluetooth {mac[:8]}") + " (BT - connetti dal sistema)"
                    result.append({
                        "type": "bluetooth",
                        "mac": mac,
                        "name": name,
                        "value": f"bt_{mac.replace(':', '')}",
                    })
    except (FileNotFoundError, subprocess.TimeoutExpired, Exception):
        pass
    return result


def get_default_input_device() -> Optional[int]:
    """Ritorna l'indice del microfono di default."""
    import sounddevice as sd
    try:
        default = sd.default.device
        if default is not None:
            v = default[0]
            return int(v) if v is not None else None
    except (TypeError, IndexError, AttributeError):
        pass
    return None


def get_device_supporting_input(device_id: Optional[int], sample_rate: int) -> int | None:
    """Ritorna un device_id valido per input (microfono)."""
    import sounddevice as sd
    if device_id is not None:
        try:
            dev = sd.query_devices(device_id)
            if dev.get("max_input_channels", 0) > 0:
                return device_id
        except Exception:
            pass
    return get_default_input_device()
