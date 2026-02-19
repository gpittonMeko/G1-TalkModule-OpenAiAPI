#!/usr/bin/env python3
"""Test device listing."""
try:
    from talk_module.audio.device_utils import list_microphones, list_speakers
    m = list_microphones()
    s = list_speakers()
    print("OK mics:", len(m), "spks:", len(s))
except Exception as e:
    print("ERR:", type(e).__name__, str(e))
