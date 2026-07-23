"""
Utility per elencare dispositivi audio.
- Integrati (dentro la macchina: G1, laptop, Jetson)
- USB / Jack (esterni)
- Bluetooth (se disponibile)
- Rete WiFi (client web)
"""

import re
import subprocess
import sys
import time
from typing import Any, Optional

# MAC Bluetooth (12 hex, separatori : o -)
_BT_MAC_RE = re.compile(r"^([0-9A-Fa-f]{2})[:-]([0-9A-Fa-f]{2})[:-]([0-9A-Fa-f]{2})[:-]([0-9A-Fa-f]{2})[:-]([0-9A-Fa-f]{2})[:-]([0-9A-Fa-f]{2})$")


def _run_text(cmd: list[str], timeout: float = 8.0) -> tuple[str, str]:
    """Esegue comando; ritorna (stdout, stderr) o ('','') se fallisce."""
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return (p.stdout or "").strip(), (p.stderr or "").strip()
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return "", ""


def probe_system_audio_hardware() -> dict[str, Any]:
    """
    Scansione lato OS (Jetson/Linux): ALSA, USB, Pulse/PipeWire.
    Non sostituisce PortAudio: serve debug e verifica cosa c'è collegato.
    """
    out: dict[str, Any] = {
        "platform": sys.platform,
        "arecord_l": "",
        "aplay_l": "",
        "asound_cards": "",
        "lsusb": "",
        "pulse_sources": "",
        "pulse_sinks": "",
    }
    if sys.platform != "linux":
        return out
    out["arecord_l"], _ = _run_text(["arecord", "-l"])
    out["aplay_l"], _ = _run_text(["aplay", "-l"])
    try:
        from pathlib import Path

        p = Path("/proc/asound/cards")
        if p.is_file():
            out["asound_cards"] = p.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        pass
    u, _ = _run_text(["lsusb"])
    lines = [ln for ln in u.splitlines() if any(
        k in ln.lower() for k in ("audio", "sound", "headset", "speaker", "webcam", "logitech", "c-media", "focusrite", "blue", "jbl")
    )]
    out["lsusb"] = "\n".join(lines[:40]) if lines else u[:2000]
    ps, _ = _run_text(["pactl", "list", "short", "sources"])
    out["pulse_sources"] = ps[:4000]
    pk, _ = _run_text(["pactl", "list", "short", "sinks"])
    out["pulse_sinks"] = pk[:4000]
    return out

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
    if any(x in n for x in ("jetson", "tegra", "ape", "orin", "xavier", "nx", "dmic", "dmics")):
        return "builtin"
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
    # Su Linux Pulse/PipeWire "default" è spesso l'unica voce reale → non escludere
    if sys.platform == "win32" and n.strip() in ("default", "sysdefault") and "usb" not in n:
        return False
    return True


def _is_physical_speaker(name: str) -> bool:
    """Esclude solo dispositivi chiaramente virtuali."""
    n = name.lower()
    skip = ("null", "monitor", "loopback", "vb-", "virtual")
    if any(s in n for s in skip):
        return False
    if sys.platform == "win32" and n.strip() in ("default", "sysdefault") and "usb" not in n:
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


def _list_alsa_hardware(capture: bool) -> list[dict]:
    """Fallback Linux quando PortAudio non è installato ma ALSA vede l'hardware."""
    if sys.platform != "linux":
        return []
    text, _ = _run_text(["arecord" if capture else "aplay", "-l"])
    result: list[dict] = []
    seen: set[int] = set()
    for line in text.splitlines():
        match = re.search(
            r"card\s+(\d+):\s*([^\[]+)\[([^\]]+)\],\s*device\s+\d+:\s*([^\[]+)",
            line,
            re.IGNORECASE,
        )
        if not match:
            continue
        card = int(match.group(1))
        if card in seen:
            continue
        seen.add(card)
        short_name = match.group(2).strip()
        long_name = match.group(3).strip()
        device_name = match.group(4).strip()
        name = f"{long_name} — {device_name}" if device_name else long_name
        dtype = _device_type(f"{short_name} {name}", capture, not capture)
        result.append(
            {
                "index": card,
                "name": name,
                "input_channels": 1 if capture else 0,
                "output_channels": 0 if capture else 1,
                "sample_rate": 48000,
                "hostapi": -1,
                "is_default_input": False,
                "is_default_output": False,
                "device_type": dtype,
                "is_physical_mic": capture,
                "is_physical_speaker": not capture,
                "backend": "alsa",
            }
        )
    return result


def list_microphones(physical_only: bool = True) -> list[dict]:
    """Lista microfoni: integrati, USB, bluetooth, jack."""
    try:
        devs = [d for d in list_audio_devices() if d.get("input_channels", 0) > 0]
    except (ImportError, OSError):
        devs = _list_alsa_hardware(capture=True)
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
    try:
        devs = [d for d in list_audio_devices() if d.get("output_channels", 0) > 0]
    except (ImportError, OSError):
        devs = _list_alsa_hardware(capture=False)
    if physical_only:
        physical = [d for d in devs if d.get("is_physical_speaker", True)]
        if physical:
            devs = physical
    # Ordina: integrati > USB > Bluetooth > Jack > altro > virtual
    order = {"builtin": 0, "usb": 1, "bluetooth": 2, "jack": 3, "headphone": 2, "speaker": 2, "other": 4, "hdmi": 5, "virtual": 6}
    devs.sort(key=lambda d: (order.get(d.get("device_type", ""), 7), -d.get("output_channels", 0)))
    return devs


def _parse_bluetoothctl_devices(stdout: str) -> list[dict]:
    """Parsa output di `bluetoothctl devices` (accoppiati + eventuali visti in scan)."""
    result: list[dict] = []
    for line in (stdout or "").strip().split("\n"):
        if "Device" not in line:
            continue
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
    return result


def list_bluetooth_devices_available() -> list[dict]:
    """
    Lista dispositivi Bluetooth già noti a BlueZ (di solito accoppiati), senza scan.
    Se connessi, appaiono anche in PortAudio.
    """
    try:
        out = subprocess.run(
            ["bluetoothctl", "devices"],
            capture_output=True,
            text=True,
            timeout=3,
        )
        if out.returncode != 0:
            return []
        return _parse_bluetoothctl_devices(out.stdout)
    except (FileNotFoundError, subprocess.TimeoutExpired, Exception):
        return []


def scan_bluetooth_devices(duration_sec: int = 10) -> tuple[list[dict], str]:
    """
    Attiva discovery Bluetooth per qualche secondo, poi elenca i device noti a BlueZ
    (accoppiati + rilevati in prossimità). Richiede `bluetoothctl` (pacchetto bluez) e,
    su Linux, di solito l’utente del processo nel gruppo `bluetooth` o permessi adeguati.
    """
    note = ""
    duration_sec = max(3, min(int(duration_sec), 60))
    try:
        proc = subprocess.Popen(
            ["bluetoothctl", "scan", "on"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
        )
    except FileNotFoundError:
        return [], "bluetoothctl non trovato (installa bluez: apt install bluez)"
    except Exception as e:
        return [], str(e)[:200]

    time.sleep(float(duration_sec))
    try:
        proc.terminate()
        proc.wait(timeout=4)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass

    try:
        subprocess.run(
            ["bluetoothctl", "scan", "off"],
            capture_output=True,
            text=True,
            timeout=6,
        )
    except Exception:
        pass

    try:
        out = subprocess.run(
            ["bluetoothctl", "devices"],
            capture_output=True,
            text=True,
            timeout=8,
        )
        if out.returncode != 0:
            err = (out.stderr or "").strip()
            return [], (err or "bluetoothctl devices fallito (permessi? gruppo bluetooth)")
        devs = _parse_bluetoothctl_devices(out.stdout)
        if not devs:
            note = "Nessun dispositivo in elenco. Metti in pairing il dispositivo e riprova, o accoppia da Impostazioni."
        return devs, note
    except Exception as e:
        return [], str(e)[:200]


def normalize_bluetooth_mac(mac: str) -> Optional[str]:
    """Normalizza MAC (AA:BB:... o 12 hex senza separatori) o None se non valido."""
    raw = (mac or "").strip()
    compact = raw.replace(":", "").replace("-", "")
    if len(compact) == 12 and re.match(r"^[0-9A-Fa-f]{12}$", compact):
        return ":".join(compact[i : i + 2].upper() for i in range(0, 12, 2))
    s = raw.replace("-", ":")
    if not _BT_MAC_RE.match(s):
        return None
    parts = s.upper().split(":")
    return ":".join(parts)


def bluetooth_control_device(action: str, mac: str) -> tuple[bool, str]:
    """
    Comandi BlueZ via bluetoothctl sul server (Linux/Jetson).
    action: trust | pair | connect | disconnect | pair_connect (trust+pair+connect)
    Serve utente del processo nel gruppo `bluetooth` o permessi equivalenti.
    PIN / conferme sul dispositivo possono essere necessari (non gestibili da web).
    """
    mac_n = normalize_bluetooth_mac(mac)
    if not mac_n:
        return False, "MAC non valido (usa AA:BB:CC:DD:EE:FF)"

    action = (action or "").strip().lower()
    allowed = {"trust", "pair", "connect", "disconnect", "pair_connect"}
    if action not in allowed:
        return False, f"Azione non supportata: {action}"

    def _run(args: list[str], timeout: float = 120.0) -> subprocess.CompletedProcess:
        return subprocess.run(
            args,
            capture_output=True,
            text=True,
            timeout=timeout,
        )

    try:
        if action == "pair_connect":
            msgs: list[str] = []
            ok_connect = False
            for cmd, label in (
                (["bluetoothctl", "trust", mac_n], "trust"),
                (["bluetoothctl", "pair", mac_n], "pair"),
                (["bluetoothctl", "connect", mac_n], "connect"),
            ):
                p = _run(cmd, timeout=120.0)
                tail = ((p.stdout or "") + (p.stderr or ""))[-400:].strip()
                msgs.append(f"{label}(rc={p.returncode}): {tail}")
                if label == "connect":
                    ok_connect = p.returncode == 0 or "successful" in tail.lower() or "connected: yes" in tail.lower()
            msg = " | ".join(msgs)[-900:]
            return ok_connect, msg

        p = _run(["bluetoothctl", action, mac_n], timeout=120.0)
        out = ((p.stdout or "") + (p.stderr or "")).strip()
        ok = p.returncode == 0 or (
            action == "pair" and ("already" in out.lower() or "succeeded" in out.lower())
        )
        return ok, (out[-700:] if out else f"bluetoothctl {action}: rc={p.returncode}")
    except FileNotFoundError:
        return False, "bluetoothctl non trovato (apt install bluez)"
    except subprocess.TimeoutExpired:
        return False, "Timeout: dispositivo spento, fuori portata, o richiede PIN sul telefono/cassa"


def _find_pulse_portaudio_index() -> Optional[int]:
    """Find the 'pulse' or 'default' input device in PortAudio (for PulseAudio routing)."""
    import sounddevice as sd
    for i, d in enumerate(sd.query_devices()):
        if d.get("max_input_channels", 0) > 0 and d.get("name", "").strip().lower() == "pulse":
            return i
    for i, d in enumerate(sd.query_devices()):
        if d.get("max_input_channels", 0) > 0 and d.get("name", "").strip().lower() == "default":
            return i
    return None


def _set_pulse_default_source_usb() -> bool:
    """On Linux, set PulseAudio default source to USB mic (allows shared access)."""
    if sys.platform != "linux":
        return False
    try:
        out, _ = _run_text(["pactl", "list", "sources", "short"])
        for line in (out or "").splitlines():
            lower = line.lower()
            if "usb" in lower and "monitor" not in lower:
                src_name = line.split("\t")[1] if "\t" in line else line.split()[1]
                subprocess.run(["pactl", "set-default-source", src_name],
                               capture_output=True, timeout=3)
                return True
    except Exception:
        pass
    return False


def ensure_pulse_usb_microphone_source() -> Optional[str]:
    """Rende disponibile e predefinita la sorgente USB/DJI, ricreando il profilo se necessario."""
    if sys.platform != "linux":
        return None

    def find_source() -> Optional[str]:
        out, _ = _run_text(["pactl", "list", "short", "sources"])
        fallback = None
        for line in out.splitlines():
            lowered = line.lower()
            if "monitor" in lowered:
                continue
            fields = line.split()
            if len(fields) < 2:
                continue
            if "dji" in lowered or "wireless_mic" in lowered:
                return fields[1]
            if "usb" in lowered and fallback is None:
                fallback = fields[1]
        return fallback

    source = find_source()
    if not source:
        cards, _ = _run_text(["pactl", "list", "short", "cards"])
        card_name = None
        for line in cards.splitlines():
            lowered = line.lower()
            if "dji" in lowered or "wireless_mic" in lowered:
                fields = line.split()
                if len(fields) >= 2:
                    card_name = fields[1]
                    break
        if card_name:
            _run_text(["pactl", "set-card-profile", card_name, "off"])
            time.sleep(0.35)
            _run_text(["pactl", "set-card-profile", card_name, "input:analog-stereo"])
            time.sleep(0.5)
            source = find_source()

    if source:
        _run_text(["pactl", "set-default-source", source])
        _run_text(["pactl", "set-source-mute", source, "0"])
        _run_text(["pactl", "set-source-volume", source, "100%"])
    return source


def resolve_configured_microphone_index(mic_cfg: Optional[dict]) -> Optional[int]:
    """
    Indice PortAudio per il microfono salvato in config/audio_devices.json.
    Se device_id non è valido o non ha ingresso, cerca per nome o primo dispositivo USB.
    Su Linux usa PulseAudio per evitare lock esclusivi ALSA.
    """
    if not mic_cfg or mic_cfg.get("type") != "local":
        return None

    # On Linux with PulseAudio: always prefer routing through PulseAudio
    # to avoid ALSA exclusive locks that block all other audio processes
    if sys.platform == "linux":
        pulse_idx = _find_pulse_portaudio_index()
        if pulse_idx is not None:
            _set_pulse_default_source_usb()
            print(f"[Audio] Using PulseAudio device (index={pulse_idx}) for shared mic access", flush=True)
            return pulse_idx

    raw = mic_cfg.get("device_id")
    try:
        want = int(raw) if raw is not None and str(raw).strip() != "" else None
    except (TypeError, ValueError):
        want = None
    import sounddevice as sd

    if want is not None:
        try:
            dev = sd.query_devices(want)
            if dev.get("max_input_channels", 0) > 0:
                return want
        except Exception:
            pass
    hint = (mic_cfg.get("name") or "").strip().lower()
    hint_compact = re.sub(r"\s*\([^)]*\)\s*$", "", hint).strip()
    mics = list_microphones(physical_only=False)
    for d in mics:
        if d.get("input_channels", 0) <= 0:
            continue
        n = (d.get("name") or "").lower()
        if hint and (hint in n or (hint_compact and hint_compact in n) or n in hint):
            return d.get("index")
    if hint_compact:
        toks = [t for t in re.split(r"[^a-z0-9]+", hint_compact) if len(t) >= 4]
        for d in mics:
            if d.get("input_channels", 0) <= 0:
                continue
            n = (d.get("name") or "").lower()
            if any(t in n for t in toks):
                return d.get("index")
    for d in mics:
        if d.get("device_type") == "usb" and d.get("input_channels", 0) > 0:
            return d.get("index")
    return get_default_input_device()


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
    """Ritorna un device_id valido per input (microfono).
    Su Linux preferisce PulseAudio per evitare lock ALSA esclusivi."""
    import sounddevice as sd
    if device_id is not None:
        try:
            dev = sd.query_devices(device_id)
            if dev.get("max_input_channels", 0) > 0:
                return device_id
        except Exception:
            pass
    # Fallback: PulseAudio on Linux
    if sys.platform == "linux":
        pulse_idx = _find_pulse_portaudio_index()
        if pulse_idx is not None:
            _set_pulse_default_source_usb()
            return pulse_idx
    return get_default_input_device()
