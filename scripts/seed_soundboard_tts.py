#!/usr/bin/env python3
"""
Genera audio TTS + effetto robot per ogni voce in config/soundboard_script.json
e scrive config/soundboard.json (20 slot).

Richiede: OPENAI_API_KEY in .env, dipendenze (openai, pydub, numpy).
TTS: voce naturale per clean, voce robot (echo) per traccia robot + effetto ring_mod/bitcrush.

Uso dalla root del repo:
  python scripts/seed_soundboard_tts.py
  python scripts/seed_soundboard_tts.py --preset ring_mod   # solo ring modulator
  python scripts/seed_soundboard_tts.py --preset bitcrush  # solo bitcrusher
  python scripts/seed_soundboard_tts.py --preset robot_full # ring+bitcrush (default)
"""

from __future__ import annotations

import argparse
import base64
import json
import sys
from pathlib import Path

_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_root))

from dotenv import load_dotenv

load_dotenv(_root / ".env")

from talk_module.audio_robot_effect import apply_robot_effect_base64
from talk_module.config import settings
from talk_module.tts.openai_tts import TTSClient

SCRIPT_PATH = _root / "config" / "soundboard_script.json"
OUT_PATH = _root / "config" / "soundboard.json"
SLOT_COUNT = 20


def main() -> int:
    ap = argparse.ArgumentParser(description="Genera soundboard TTS + effetto robot")
    ap.add_argument("--preset", choices=["telephone", "ring_mod", "bitcrush", "robot_full"], default=None, help="Effetto robot (default: env ROBOT_EFFECT_PRESET o robot_full)")
    args = ap.parse_args()
    preset = args.preset or settings.robot_effect_preset or "robot_full"

    errs = settings.validate()
    if errs:
        print("Errore configurazione:", "; ".join(errs))
        return 1
    if not SCRIPT_PATH.exists():
        print("Manca", SCRIPT_PATH)
        return 1
    data = json.loads(SCRIPT_PATH.read_text(encoding="utf-8"))
    entries = data.get("entries") or []
    tts_natural = TTSClient(voice=settings.tts_voice)
    tts_robot = TTSClient(voice=settings.tts_voice_robot)
    print(f"Preset effetto: {preset} | Voce naturale: {settings.tts_voice} | Voce robot: {settings.tts_voice_robot}")
    slots: list[dict] = []
    for i in range(SLOT_COUNT):
        if i < len(entries):
            e = entries[i]
            icon = str(e.get("icon", "🎤"))[:4]
            label = str(e.get("label_corto", f"Comando {i+1}")).strip()[:280]
            text = str(e.get("testo_tts", label)).strip()
            if not text:
                slots.append(
                    {
                        "icon": icon,
                        "text": label or f"Comando {i+1}",
                        "audio_base64": "",
                        "format": "webm",
                        "audio_base64_clean": "",
                        "format_clean": "mp3",
                    }
                )
                continue
            print(f"[{i+1}/{SLOT_COUNT}] TTS: {label[:50]}...")
            raw_wav_natural = tts_natural.synthesize(text, format="wav")
            raw_wav_robot = tts_robot.synthesize(text, format="wav")
            raw_wav = raw_wav_natural
            if not raw_wav:
                print("  ! TTS vuoto, slot senza audio")
                slots.append(
                    {
                        "icon": icon,
                        "text": label,
                        "audio_base64": "",
                        "format": "webm",
                        "audio_base64_clean": "",
                        "format_clean": "wav",
                    }
                )
                continue
            b64_clean = base64.b64encode(raw_wav).decode()
            b64_for_effect = base64.b64encode(raw_wav_robot or raw_wav).decode()
            b64_robot, fmt_r = apply_robot_effect_base64(b64_for_effect, "wav", preset=preset)
            slots.append(
                {
                    "icon": icon,
                    "text": label,
                    "audio_base64": b64_robot,
                    "format": fmt_r,
                    "audio_base64_clean": b64_clean,
                    "format_clean": "wav",
                }
            )
        else:
            slots.append(
                {
                    "icon": "🎤",
                    "text": f"Comando {i+1}",
                    "audio_base64": "",
                    "format": "webm",
                    "audio_base64_clean": "",
                    "format_clean": "mp3",
                }
            )
    OUT_PATH.write_text(json.dumps({"slots": slots}, indent=2, ensure_ascii=False), encoding="utf-8")
    n_robot = sum(1 for s in slots if s.get("audio_base64"))
    n_clean = sum(1 for s in slots if s.get("audio_base64_clean"))
    print("Scritto", OUT_PATH, "—", n_robot, "slot con traccia robot,", n_clean, "con traccia naturale.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
