#!/usr/bin/env python3
"""Quick 3-second recording test via PulseAudio."""
import sounddevice as sd
import numpy as np

dev = None
rate = 44100
for i, d in enumerate(sd.query_devices()):
    if d.get("name", "").strip().lower() == "pulse" and d.get("max_input_channels", 0) > 0:
        dev = i
        rate = int(d.get("default_samplerate", 44100))
        break

if dev is None:
    print("ERRORE: device pulse non trovato")
    raise SystemExit(1)

print(f"Device: [{dev}] pulse  rate={rate}")
print("Registro 3 secondi... PARLA ORA!")
audio = sd.rec(int(rate * 3), samplerate=rate, channels=1, dtype="float32", device=dev)
sd.wait()
rms = float(np.sqrt(np.mean(audio ** 2)))
peak = float(np.max(np.abs(audio)))
db = max(-60, 20 * np.log10(rms + 1e-10))
print(f"RMS={rms:.6f}  Peak={peak:.6f}  dB={db:+.1f}")
if rms > 0.005:
    print("RISULTATO: Audio catturato! Il microfono funziona.")
elif rms > 0.0005:
    print("RISULTATO: Segnale molto basso ma presente.")
else:
    print("RISULTATO: Silenzio totale - il microfono non cattura audio.")
