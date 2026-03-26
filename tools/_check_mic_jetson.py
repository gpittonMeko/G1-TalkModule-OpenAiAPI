#!/usr/bin/env python3
"""Esegui sulla Jetson: elenco mic PortAudio + arecord."""
import sys
from pathlib import Path

# tools/_check_mic_jetson.py -> repo root = parent.parent
root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(root))

print("=== arecord -l (ALSA) ===")
import subprocess

try:
    subprocess.run(["arecord", "-l"], timeout=5)
except Exception as e:
    print(e)

print("\n=== PortAudio (list_microphones) ===")
try:
    from talk_module.audio.device_utils import list_microphones

    m = list_microphones(physical_only=False)
    print("count:", len(m))
    for d in m[:20]:
        print(" ", d.get("index"), "|", d.get("name"), "|", d.get("device_type"))
except Exception as e:
    print("ERR:", e)
