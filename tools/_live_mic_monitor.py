#!/usr/bin/env python3
"""Monitor livello microfono USB in tempo reale (esegui sulla Jetson).

Usa PulseAudio per evitare conflitti di accesso esclusivo ALSA
quando il server ha già il dispositivo aperto.
"""
import sys
import subprocess
import numpy as np
import sounddevice as sd


def set_pulse_usb_default():
    """Set the USB mic as PulseAudio default source."""
    try:
        out = subprocess.check_output(["pactl", "list", "sources", "short"], text=True)
        for line in out.strip().splitlines():
            if "usb" in line.lower() and "monitor" not in line.lower():
                src_name = line.split("\t")[1]
                subprocess.run(["pactl", "set-default-source", src_name], check=True)
                return src_name
    except Exception:
        pass
    return None


def find_pulse_device():
    """Find the 'pulse' or 'default' device in sounddevice."""
    for i, d in enumerate(sd.query_devices()):
        if d.get("max_input_channels", 0) > 0:
            name = d.get("name", "").strip().lower()
            if name == "pulse":
                return i, d
    for i, d in enumerate(sd.query_devices()):
        if d.get("max_input_channels", 0) > 0:
            name = d.get("name", "").strip().lower()
            if name == "default":
                return i, d
    return None, None


def find_usb_mic():
    """Trova il primo microfono USB con canali di ingresso (accesso diretto ALSA)."""
    for i, d in enumerate(sd.query_devices()):
        if d.get("max_input_channels", 0) > 0 and "usb" in d.get("name", "").lower():
            return i, d
    return None, None


# 1) Try direct USB mic first
DEVICE, dev_info = find_usb_mic()

# 2) If not available (locked by server), route through PulseAudio
if DEVICE is None:
    pulse_src = set_pulse_usb_default()
    DEVICE, dev_info = find_pulse_device()
    if DEVICE is not None and pulse_src:
        print(f"\n  (Accesso diretto bloccato, uso PulseAudio -> {pulse_src})")

if DEVICE is None:
    print("\n  ERRORE: Nessun microfono trovato!")
    print("  Dispositivi disponibili:")
    for i, d in enumerate(sd.query_devices()):
        if d.get("max_input_channels", 0) > 0:
            print(f"    [{i}] {d['name']}  ch={d['max_input_channels']}")
    sys.exit(1)

name = dev_info.get("name", "?")
RATE = int(dev_info.get("default_samplerate", 48000))
BLOCK = int(RATE * 0.05)  # 50ms chunks
BAR_WIDTH = 60

print(f"\n  Mic: [{DEVICE}] {name}  rate={RATE}")
print(f"  Premi Ctrl+C per uscire.\n")

try:
    with sd.InputStream(device=DEVICE, channels=1, samplerate=RATE,
                        blocksize=BLOCK, dtype="float32") as stream:
        while True:
            data, _ = stream.read(BLOCK)
            audio = data.squeeze()
            rms = float(np.sqrt(np.mean(audio ** 2)))
            peak = float(np.max(np.abs(audio)))
            db = max(-60, 20 * np.log10(rms + 1e-10))
            filled = int((db + 60) / 60 * BAR_WIDTH)
            filled = max(0, min(BAR_WIDTH, filled))
            bar = "#" * filled + "-" * (BAR_WIDTH - filled)
            if peak > 0.5:
                color = "\033[91m"
            elif rms > 0.02:
                color = "\033[92m"
            elif rms > 0.005:
                color = "\033[93m"
            else:
                color = "\033[90m"
            sys.stdout.write(f"\r  {color}[{bar}]\033[0m  RMS={rms:.4f}  Peak={peak:.4f}  dB={db:+.1f}  ")
            sys.stdout.flush()
except KeyboardInterrupt:
    print("\n\n  Stop.")
except Exception as e:
    print(f"\n  Errore: {e}")
