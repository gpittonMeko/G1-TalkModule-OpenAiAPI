#!/usr/bin/env python3
"""Analyze a raw PCM file for audio level."""
import numpy as np
import sys

data = np.fromfile("/tmp/_test2.raw", dtype=np.int16)
audio = data.astype(np.float32) / 32768.0
rms = float(np.sqrt(np.mean(audio ** 2)))
peak = float(np.max(np.abs(audio)))
db = max(-60, 20 * np.log10(rms + 1e-10))
samples = len(audio)
duration = samples / 48000.0
print(f"Campioni: {samples}  Durata: {duration:.2f}s")
print(f"RMS={rms:.6f}  Peak={peak:.6f}  dB={db:+.1f}")
if rms > 0.005:
    print("RISULTATO: Audio catturato! Il microfono FUNZIONA.")
elif rms > 0.0005:
    print("RISULTATO: Segnale basso ma presente.")
else:
    print("RISULTATO: Silenzio totale.")
