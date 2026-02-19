#!/usr/bin/env python3
"""Test rapido: registra 2 sec dal mic configurato e verifica livello."""
import sounddevice as sd
import numpy as np

device_id = 0  # USB Headset H361
sr = 44100  # USB Headset supporta 44100
print("Registro 2 secondi... parla ora!")
rec = sd.rec(int(2 * sr), samplerate=sr, channels=1, device=device_id, dtype="float32")
sd.wait()
rms = float(np.sqrt(np.mean(rec**2)))
print(f"RMS = {rms:.4f}")
print("OK - microfono funziona" if rms > 0.001 else "ATTENZIONE - audio molto basso o silenzio")
