#!/usr/bin/env python3
"""Test microfono: elenca device, registra 2 sec, mostra livello."""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import sounddevice as sd
import numpy as np

print("=== PortAudio input devices ===")
for i, d in enumerate(sd.query_devices()):
    if d.get("max_input_channels", 0) > 0:
        rate = d.get("default_samplerate", 0)
        print(f"  [{i}] {d['name']}  ch={d['max_input_channels']}  rate={rate}")
print(f"  Default device: {sd.default.device}")

cfg_path = Path(__file__).resolve().parents[1] / "config" / "audio_devices.json"
print(f"\n=== Config: {cfg_path} ===")
if cfg_path.exists():
    cfg = json.loads(cfg_path.read_text())
    mic = cfg.get("microphone", {})
    print(f"  mic type={mic.get('type')}  device_id={mic.get('device_id')}  name={mic.get('name')}")
else:
    print("  (file non trovato)")

from talk_module.audio.device_utils import resolve_configured_microphone_index
resolved = resolve_configured_microphone_index(mic if cfg_path.exists() else None)
print(f"  -> resolve_configured_microphone_index = {resolved}")

dev_idx = resolved if resolved is not None else 0
dev_info = sd.query_devices(dev_idx)
rate = int(dev_info.get("default_samplerate", 48000))
print(f"\n=== Test registrazione 2 sec da device [{dev_idx}] rate={rate} ===")
try:
    rec = sd.rec(rate * 2, samplerate=rate, channels=1, device=dev_idx, dtype="float32")
    sd.wait()
    audio = rec.squeeze()
    rms = float(np.sqrt(np.mean(audio ** 2)))
    peak = float(np.max(np.abs(audio)))
    print(f"  RMS  = {rms:.6f}")
    print(f"  Peak = {peak:.6f}")
    print(f"  Samples = {len(audio)}")
    if rms < 0.001:
        print("  >>> SILENZIO - il microfono non cattura audio!")
    else:
        print(f"  >>> AUDIO OK (RMS={rms:.4f})")
except Exception as e:
    print(f"  ERRORE: {e}")
